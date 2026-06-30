"""The brain — judgment + reflection.

This is the layer that "thinks" about a candidate in light of memory, and that
writes the narrative after a trade resolves. It has two implementations behind
one interface:

  • Deterministic (default, FREE) — rules driven by the symbol's own track
    record and retrieved lessons. No API cost. This is what proves whether the
    strategy has an edge before any money is spent on inference.

  • Claude (optional, OFF) — delegates the same two calls to an LLM for nuanced
    reasoning over the journal prose. Enabled only when LLM_ENABLED is true.

Keeping both behind `judge()` / `reflect()` means turning the LLM on later is a
config flip, not a rewrite.
"""
from __future__ import annotations

import logging
from typing import Any

from v2 import journal, store
from v2.config import LLM_ENABLED

log = logging.getLogger(__name__)


# ---------- Judgment --------------------------------------------------------

def judge(candidate: dict[str, Any], retrieval: dict[str, Any]) -> dict[str, Any]:
    """Decide take/skip for a candidate. Returns a structured decision:
        {take: bool, confidence: low|medium|high, size: none|quarter|half|full,
         rationale: str, source: str}
    """
    if LLM_ENABLED:
        try:
            from v2 import llm
            verdict = llm.judge(candidate, journal.render_memory_block(retrieval))
            if verdict:
                return verdict
            log.warning("LLM judge returned nothing — falling back to deterministic")
        except Exception as e:  # never let an LLM failure stop a scan
            log.warning("LLM judge failed (%s) — using deterministic", e)
    return _judge_deterministic(candidate, retrieval)


_CONF_FOR = {"quarter": "low", "half": "medium", "full": "high"}


def _graduated_size(score: int, n_decided: int, wr: float | None,
                    avg_r: float | None, meaningful: bool) -> tuple[str, str]:
    """Graduated PROBATIONARY sizing (Phase 3 — replaces the hard 5-trade
    cold-start skip that deadlocked the audit). A structurally-valid candidate is
    never skipped for *lack* of history; instead size ramps with the decided-
    trade count and the symbol's win rate:

      • cold (0–1 decided): quarter — tiny probation stake, just enough to learn.
      • thin (2–4 decided):  quarter single-setup, half dual-confluence.
      • meaningful (≥5):     win-rate-driven — full at ≥60%/+avg, half at ≥50%,
                             quarter otherwise.

    (A proven-BAD meaningful record is hard-skipped upstream, not here.)
    Returns (size, reason_fragment).
    """
    dual = score >= 100
    if not meaningful:
        if n_decided < 2:
            return "quarter", f"probation ({n_decided} decided) — tiny stake to learn"
        return ("half" if dual else "quarter",
                f"thin sample ({n_decided} decided) — ramping")
    if wr is not None and wr >= 0.6 and (avg_r or 0) > 0:
        return "full", f"strong record ({_pct(wr)}, {_r(avg_r)})"
    if wr is not None and wr >= 0.5:
        return "half", f"decent record ({_pct(wr)})"
    return "quarter", f"weak-ish record ({_pct(wr)}) — trim"


def _judge_deterministic(candidate: dict[str, Any], retrieval: dict[str, Any]) -> dict[str, Any]:
    """Track-record-driven judgment. The free, always-on path.

    Logic, in plain terms:
      • Structure must be there — score 100 (both setups agree) sizes up faster
        than 50 (one setup), but neither is skipped for lack of history.
      • Size ramps with the decided-trade count (graduated probation) and the
        symbol's win rate — see _graduated_size. This fixes the audit's cold-
        start deadlock where single-setups could never accrue a record.
      • A clearly bad track record (meaningful sample, sub-35% win rate or
        negative expectancy) is a hard skip — this replaces v1's separate
        cooling-off blacklist with one coherent rule.
    """
    score = candidate["score"]
    stats = retrieval["symbol_stats"]
    wr = stats["win_rate"]
    avg_r = stats["avg_r"]
    meaningful = stats["meaningful"]
    reasons: list[str] = [f"score {score} ({'+'.join(candidate.get('setups') or [])})",
                          f"R:R {candidate.get('rr')}"]

    # Hard skip on a proven-bad symbol (subsumes v1 cooling-off).
    if meaningful and ((wr is not None and wr < 0.35) or (avg_r is not None and avg_r < 0)):
        return _decision(False, "low", "none",
                         f"skip — {stats['symbol']} track record is negative "
                         f"(win rate {_pct(wr)}, avg {_r(avg_r)} over {stats['n_closed']} trades)")

    if score >= 100:
        reasons.append("dual-confluence (OB + BOS agree)")
    size, frag = _graduated_size(score, stats["wins"] + stats["losses"], wr, avg_r, meaningful)
    confidence = _CONF_FOR[size]
    reasons.append(frag)

    # A relevant negative lesson tempers confidence.
    neg = [l for l in retrieval["lessons"] if any(
        w in l["summary"].lower() for w in ("avoid", "lost", "stop", "fail", "chop"))]
    if neg:
        reasons.append(f"caution from lesson: {neg[0]['summary'][:80]}")
        if confidence == "high":
            confidence = "medium"

    return _decision(True, confidence, size, "; ".join(reasons))


def _decision(take: bool, confidence: str, size: str, rationale: str) -> dict[str, Any]:
    return {"take": take, "confidence": confidence, "size": size,
            "rationale": rationale, "source": "deterministic"}


def _pct(x: float | None) -> str:
    return f"{x * 100:.0f}%" if x is not None else "n/a"


def _r(x: float | None) -> str:
    return f"{x:+.2f}R" if x is not None else "n/a"


# ---------- Reflection ------------------------------------------------------

def reflect_on_closed(trades: list[dict[str, Any]]) -> int:
    """For each newly-resolved trade, write a journal entry and, where the
    outcome is instructive, a distilled lesson. Returns count journaled."""
    n = 0
    for t in trades:
        try:
            story, lesson = _reflect_one(t)
            path = journal.write_trade_entry(t, story=story, lesson=lesson)
            store.mark_journaled(t["id"], path.name)
            _maybe_distil_lesson(t)
            n += 1
        except Exception as e:
            log.warning("reflection failed for trade %s: %s", t.get("id"), e)
    return n


def _reflect_one(trade: dict[str, Any]) -> tuple[str, str]:
    if LLM_ENABLED:
        try:
            from v2 import llm
            out = llm.reflect(trade, journal.render_memory_block(
                journal.retrieve_for(_trade_as_candidate(trade))))
            if out:
                return out["story"], out["lesson"]
        except Exception as e:
            log.warning("LLM reflect failed (%s) — templated fallback", e)
    return _reflect_templated(trade)


def _reflect_templated(trade: dict[str, Any]) -> tuple[str, str]:
    outcome = trade["outcome"]
    pnl = trade.get("pnl_r")
    direction = trade["direction"]
    held = trade.get("tp1_hit")
    if outcome == "WIN_TP2":
        story = (f"Filled at {trade['entry_price']} and ran the full distance to TP2 "
                 f"({trade['tp2']}) for {pnl}R. TP1 was banked en route and the stop "
                 f"trailed to entry, so the runner had no downside.")
        lesson = ("Letting the winner run to TP2 paid off here — the 2R/3R target ladder "
                  "off a tight stop captured the full move without giving it back.")
    elif outcome == "BREAKEVEN":
        story = (f"Reached TP1, trailed the stop to entry, then reversed and stopped at "
                 f"breakeven for {pnl}R. The first target printed but the runner didn't.")
        lesson = ("TP1 hit but no follow-through — the move had less in it than 3 ATR. "
                  "Worth watching whether this symbol/regime tends to stall after TP1.")
    elif outcome == "LOSS":
        story = (f"Stopped out at {trade['original_sl']} for {pnl}R without reaching TP1. "
                 f"The retest failed to hold.")
        lesson = (f"{trade['symbol']} {direction} on {trade.get('regime')} regime failed at "
                  "the retest — if this repeats, the setup is being faded in this context.")
    else:  # EXPIRED
        story = (f"Never resolved within the holding window; force-closed at {trade.get('close_price')} "
                 f"for {pnl}R. {'TP1 was hit' if held else 'TP1 never printed'}.")
        lesson = ("Trade went nowhere — neither target nor stop. Either entry timing was early "
                  "or the setup lacked momentum.")
    return story, lesson


def _maybe_distil_lesson(trade: dict[str, Any]) -> None:
    """Promote a recurring pattern to a standalone lesson. Fires when a symbol
    has a clear, meaningful skew so the judge picks it up on future candidates."""
    stats = store.symbol_stats(trade["symbol"])
    if not stats["meaningful"]:
        return
    wr = stats["win_rate"]
    if wr is None:
        return
    if wr < 0.35:
        journal.write_lesson(
            trade["symbol"],
            f"Avoid {trade['symbol']} setups — {stats['wins']}W/{stats['losses']}L "
            f"({wr * 100:.0f}%), avg {stats['avg_r']:+.2f}R. Faded in current regime.",
            f"After {stats['n_closed']} closed trades {trade['symbol']} is a net loser. "
            "The deterministic judge will hard-skip it until the record recovers.",
        )
    elif wr >= 0.6 and (stats["avg_r"] or 0) > 0:
        journal.write_lesson(
            trade["symbol"],
            f"{trade['symbol']} is performing — {wr * 100:.0f}% win rate, "
            f"avg {stats['avg_r']:+.2f}R. Size up with confidence.",
            f"{trade['symbol']} has a positive, meaningful track record. Full size is justified.",
        )


def _trade_as_candidate(trade: dict[str, Any]) -> dict[str, Any]:
    import json as _json
    return {
        "symbol": trade["symbol"], "direction": trade["direction"],
        "setups": _json.loads(trade["setups"]) if trade.get("setups") else [],
        "regime": trade.get("regime"),
    }
