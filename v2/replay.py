"""Walk-forward replay harness → BASELINE.md.

Honest, pure-function backtest: it reuses the SAME detectors, levels and
resolution as the live bot (no separate, flattering simulator), and it never
touches the live ledger. For each symbol it walks daily history bar-by-bar,
builds a candidate on each closed bar, opens a paper trade at the modelled fill
(spread-worsened for FX), and resolves it forward with the intrabar SL-first
logic (`store.walk_trade`) — so a bar that pierces a level is recorded as a real
win/loss, not silently expired at ~0R (the audit's master bug).

Baseline trades are recorded at size_mult = 1.0 to measure the *raw setup edge*
per threshold, before any judge sizing. Output is candidate frequency and
expectancy per pair and per score threshold, plus a rejection-reason breakdown.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from config import BACKTEST_WARMUP_BARS
from market_data import Bar
from v2 import config as cfg
from v2 import datasource, signals, store

log = logging.getLogger(__name__)

_WIN = {store.OUTCOME_WIN_TP2}
_LOSS = {store.OUTCOME_LOSS}


def _instrument(symbol: str, spread_pips: float | None = None) -> signals.Instrument | None:
    if not cfg.FX_ENABLED:
        return None
    return signals.Instrument(
        symbol=symbol, pip_size=cfg.fx_pip_size(symbol),
        spread_pips=(spread_pips if spread_pips is not None
                     else cfg.fx_spread_pips(symbol)),
        equity=cfg.FX_ACCOUNT_EQUITY, risk_pct=cfg.FX_RISK_PCT,
        std_lot=cfg.FX_STD_LOT_UNITS,
    )


def _resolve_forward(trade: dict[str, Any], forward: list[Bar]) -> tuple[str | None, float]:
    """Resolve a paper trade over the bars that follow it, capped to the holding
    window so expiry fires at the right place."""
    opened = trade["_opened_dt"]
    in_window = [b for b in forward
                 if store._trading_days_between(opened, b.dt) <= cfg.EXPIRY_TRADING_DAYS]
    if not in_window:
        return None, 0.0
    now = in_window[-1].dt
    outcome, close_price = store.walk_trade(trade, in_window, now)
    return outcome, close_price


def replay_symbol(symbol: str, bars: list[Bar],
                  *, spread_pips: float | None = None,
                  max_lookback: int | None = None) -> dict[str, Any]:
    """Walk one symbol. Returns trades (resolved + still-open) and rejection
    reason counts. `spread_pips` overrides the assumed per-pair constant with a
    measured spread (OANDA baseline) — entry/R:R are then net of real cost.
    `max_lookback` caps the trailing detector window per decision so a very long
    history (OANDA 15-20yr) feeds the detectors the SAME span the live bot sees
    (period="3y") — faithfulness to production, and it keeps the walk linear.
    None = full growing prefix (unchanged; fine for the ~3yr yfinance feed)."""
    inst = _instrument(symbol, spread_pips)
    impulse = cfg.FX_OB_IMPULSE_THRESHOLD if cfg.FX_ENABLED else None
    trades: list[dict[str, Any]] = []
    rejections: dict[str, int] = {}
    open_dirs: dict[str, int] = {}  # direction -> index of bar it resolves by (dedup)

    n = len(bars)
    start = max(BACKTEST_WARMUP_BARS, 20)
    eval_days = max(0, n - start)
    for i in range(start, n):
        lo = 0 if max_lookback is None else max(0, i + 1 - max_lookback)
        window = bars[lo: i + 1]
        cand, reason = signals.build_candidate(
            symbol, window, live_price=window[-1].c, instrument=inst,
            impulse_threshold=impulse)
        if cand is None:
            rejections[reason or "unknown"] = rejections.get(reason or "unknown", 0) + 1
            continue
        direction = cand["direction"]
        # dedup: don't open a second same-direction trade while one is unresolved
        if open_dirs.get(direction, -1) > i:
            rejections["dedup_open_position"] = rejections.get("dedup_open_position", 0) + 1
            continue

        trade = {
            "symbol": symbol, "direction": direction, "setups": cand["setups"],
            "regime": cand["regime"], "score": cand["score"],
            "entry_price": cand["entry"], "stop_loss": cand["stop_loss"],
            "original_sl": cand["stop_loss"], "tp1": cand["tp1"], "tp2": cand["tp2"],
            "tp1_hit": 0, "tp1_hit_at": None,
            "opened_at": bars[i].dt.isoformat(), "_opened_dt": bars[i].dt,
        }
        # Cap the forward slice to the expiry window. Resolution only ever uses
        # bars within EXPIRY_TRADING_DAYS trading days (bar #k is >= k trading
        # days ahead), so any bar past ~3x that is filtered out regardless — the
        # cap is a no-op on the result but turns _resolve_forward's O(forward)
        # scan (with a day-by-day _trading_days_between per bar) from O(n^2) into
        # O(1) on deep OANDA history.
        fwd_cap = cfg.EXPIRY_TRADING_DAYS * 3 + 5
        outcome, close_price = _resolve_forward(trade, bars[i + 1: i + 1 + fwd_cap])
        if outcome is not None:
            trade["outcome"] = outcome
            trade["raw_r"] = round(store._pnl_r(
                trade["entry_price"], trade["original_sl"], close_price, direction), 3)
            # Hold the symbol+direction busy across the trade's lifetime so we
            # don't stack duplicate entries on the same setup (matches live dedup).
            open_dirs[direction] = min(i + cfg.EXPIRY_TRADING_DAYS, n)
        else:
            trade["outcome"] = None
            trade["raw_r"] = None
            open_dirs[direction] = n  # still open through end of data
        trades.append(trade)

    return {"symbol": symbol, "trades": trades, "rejections": rejections,
            "eval_days": eval_days}


def _agg(trades: list[dict[str, Any]], min_score: int) -> dict[str, Any]:
    sel = [t for t in trades if t["score"] >= min_score and t["outcome"] is not None]
    wins = sum(1 for t in sel if t["outcome"] in _WIN)
    losses = sum(1 for t in sel if t["outcome"] in _LOSS)
    decided = wins + losses
    rs = [t["raw_r"] for t in sel if t["raw_r"] is not None]
    by_outcome: dict[str, int] = {}
    for t in sel:
        by_outcome[t["outcome"]] = by_outcome.get(t["outcome"], 0) + 1
    return {
        "n_resolved": len(sel),
        "wins": wins, "losses": losses,
        "win_rate": (wins / decided) if decided else None,
        "avg_r": (sum(rs) / len(rs)) if rs else None,
        "total_r": round(sum(rs), 2) if rs else 0.0,
        "by_outcome": by_outcome,
    }


async def run_replay() -> dict[str, Any]:
    source = datasource.get_data_source()
    per_symbol: dict[str, Any] = {}
    all_trades: list[dict[str, Any]] = []
    all_rej: dict[str, int] = {}
    total_eval_days = 0

    for symbol in source.symbols():
        bars = await source.fetch_daily(symbol)
        if len(bars) < BACKTEST_WARMUP_BARS + 5:
            log.warning("replay: %s only %d bars — skipping", symbol, len(bars))
            continue
        res = replay_symbol(symbol, bars)
        per_symbol[symbol] = res
        all_trades += [{**t, "symbol": symbol} for t in res["trades"]]
        for k, v in res["rejections"].items():
            all_rej[k] = all_rej.get(k, 0) + v
        total_eval_days += res["eval_days"]

    if hasattr(source, "aclose"):
        await source.aclose()

    # All pairs are scanned concurrently each week, so basket frequency is over
    # the CALENDAR window (the typical per-symbol eval span), not the sum of
    # per-symbol spans. Use the max per-symbol eval window as the calendar span.
    cal_days = max((r["eval_days"] for r in per_symbol.values()), default=0)
    calendar_weeks = cal_days / 5.0 if cal_days else 0

    return {
        "market": source.name,
        "per_symbol": per_symbol,
        "all_trades": all_trades,
        "rejections": all_rej,
        "total_eval_days": total_eval_days,
        "calendar_weeks": calendar_weeks,
        "thresholds": {str(s): _agg(all_trades, s) for s in (50, 100)},
    }


def _fmt_pct(x: float | None) -> str:
    return f"{x * 100:.0f}%" if x is not None else "n/a"


def _fmt_r(x: float | None) -> str:
    return f"{x:+.2f}R" if x is not None else "n/a"


def write_baseline(stats: dict[str, Any], path: str = "BASELINE.md") -> None:
    market = stats["market"]
    lines: list[str] = []
    lines.append(f"# BASELINE — {market} walk-forward replay\n")
    lines.append("Generated by `python run.py --replay`. Uses the live detectors, levels and "
                 "intrabar SL-first resolution (no separate simulator). Baseline trades are "
                 "recorded at size_mult=1.0 to show the **raw setup edge** before judge sizing. "
                 "FX entries are worsened by the assumed per-pair spread; R:R is post-spread.\n")
    weeks = stats.get("calendar_weeks") or 0

    lines.append("## Expectancy by score threshold (whole basket)\n")
    lines.append("| Threshold | Resolved | Win rate | Avg R | Total R | Outcomes |")
    lines.append("|-----------|---------:|---------:|------:|--------:|----------|")
    for s in ("50", "100"):
        a = stats["thresholds"][s]
        oc = ", ".join(f"{k}:{v}" for k, v in sorted(a["by_outcome"].items()))
        label = "≥50 (all)" if s == "50" else "=100 (dual)"
        lines.append(f"| {label} | {a['n_resolved']} | {_fmt_pct(a['win_rate'])} | "
                     f"{_fmt_r(a['avg_r'])} | {a['total_r']:+.2f} | {oc or '—'} |")
    if weeks:
        n50 = stats["thresholds"]["50"]["n_resolved"]
        n100 = stats["thresholds"]["100"]["n_resolved"]
        lines.append(f"\n_Basket frequency: ~{n50 / weeks:.2f} resolved ≥50 / week, "
                     f"~{n100 / weeks:.2f} dual-confluence / week "
                     f"(~{weeks:.0f} calendar weeks of history, whole basket scanned each week)._\n")

    lines.append("## Per-pair (score ≥ 50)\n")
    lines.append("| Symbol | Resolved | Win rate | Avg R | Total R |")
    lines.append("|--------|---------:|---------:|------:|--------:|")
    for sym, res in stats["per_symbol"].items():
        a = _agg(res["trades"], 50)
        lines.append(f"| {sym} | {a['n_resolved']} | {_fmt_pct(a['win_rate'])} | "
                     f"{_fmt_r(a['avg_r'])} | {a['total_r']:+.2f} |")

    lines.append("\n## Rejection reasons (why candidates didn't fire)\n")
    lines.append("| Reason | Count |")
    lines.append("|--------|------:|")
    for reason, n in sorted(stats["rejections"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {reason} | {n} |")

    still_open = sum(1 for t in stats["all_trades"] if t["outcome"] is None)
    lines.append(f"\n_{len(stats['all_trades'])} candidates opened in replay; "
                 f"{still_open} still open at end of data (excluded from expectancy)._\n")
    lines.append("\n> Expectancy here is the **raw, unsized** edge. It is NOT a recommendation to "
                 "trade — Phase 3 chooses a threshold only where expectancy stays positive.\n")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log.info("wrote %s", path)


def write_calibration(stats: dict[str, Any], path: str = "CALIBRATION.md") -> None:
    """Frequency-vs-expectancy curve across score thresholds, with a proposed
    threshold. The detector only emits scores 0/50/100, so the curve has two
    actionable points (>=50, =100). Per the brief: target ~1 trade/week ONLY
    where expectancy stays positive; otherwise pick the highest-expectancy
    threshold and say plainly the basket can't reach 1/week profitably."""
    from v2.config import FX_MIN_SCORE
    weeks = stats.get("calendar_weeks") or 0
    lines = [f"# CALIBRATION — {stats['market']}\n",
             "Frequency vs expectancy from the walk-forward replay (raw, unsized R; live "
             "detectors + honest intrabar resolution). The score threshold is the only "
             "frequency lever the detector exposes (scores are 0/50/100).\n",
             "## Threshold curve (whole basket)\n",
             "| Threshold | Resolved | Resolved/week | Win rate | Avg R (expectancy) | Verdict |",
             "|-----------|---------:|--------------:|---------:|-------------------:|---------|"]
    verdicts = {}
    for s in ("50", "100"):
        a = stats["thresholds"][s]
        per_wk = (a["n_resolved"] / weeks) if weeks else 0
        exp = a["avg_r"]
        if exp is None or a["n_resolved"] < 5:
            verdict = "unknown (thin sample)"
        elif exp > 0:
            verdict = "positive"
        else:
            verdict = "negative — do not trade"
        verdicts[s] = (per_wk, exp, verdict)
        label = "≥50 (all)" if s == "50" else "=100 (dual)"
        lines.append(f"| {label} | {a['n_resolved']} | {per_wk:.2f} | {_fmt_pct(a['win_rate'])} "
                     f"| {_fmt_r(exp)} | {verdict} |")

    p50, e50, v50 = verdicts["50"]
    p100, e100, v100 = verdicts["100"]
    lines.append("\n## Proposed threshold\n")
    # "Positive" only counts a MEANINGFUL sample whose edge is clear of noise
    # (avg R >= +0.10). +0.05R at 18% WR is within noise — not a basis to trade.
    MARGINAL = 0.10
    pos = {s: verdicts[s] for s in ("100", "50")
           if verdicts[s][2] == "positive" and (verdicts[s][1] or 0) >= MARGINAL}
    # More signals is not the goal — pick the HIGHEST-expectancy threshold, even
    # if that means < 1/week. Only drop to a looser one if expectancy holds up.
    if pos:
        best = max(pos, key=lambda s: pos[s][1])
        choice = int(best)
        freq = verdicts[best][0]
        alt = "≥50" if best == "100" else "—"
        why = (f"highest robust expectancy is at ={best} ({_fmt_r(verdicts[best][1])}, "
               f"~{freq:.1f}/week). ≥50 trades ~{p50:.1f}/week but only {_fmt_r(e50)} "
               f"(marginal, ~breakeven WR) — frequency without a clear edge, so not chosen. "
               f"~1/week at positive expectancy is approachable at =100; below 1/week is "
               f"acceptable rather than loosening into noise.")
    else:
        choice, why = 100, ("**no threshold has clearly positive (non-marginal) expectancy** "
                            "— defaulting to the most selective (100) and recommending "
                            "PAPER-ONLY until the live ledger proves an edge.")
    lines.append(f"- **FX_MIN_SCORE = {choice}** — {why}")
    lines.append(f"- Currently set in config: `FX_MIN_SCORE = {FX_MIN_SCORE}` "
                 f"(marked `# REVIEW: proposed by calibration`).\n")
    lines.append("\n## Per-pair expectancy (score ≥ 50)\n")
    lines.append("| Symbol | Resolved | Win rate | Avg R |")
    lines.append("|--------|---------:|---------:|------:|")
    for sym, res in stats["per_symbol"].items():
        a = _agg(res["trades"], 50)
        lines.append(f"| {sym} | {a['n_resolved']} | {_fmt_pct(a['win_rate'])} | {_fmt_r(a['avg_r'])} |")
    lines.append("\n> **This threshold is a PROPOSAL for your review, not a final decision.** "
                 "More signals is not the goal — positive measured expectancy is. The graduated "
                 "probationary sizing (Phase 3) lets the bot accrue a real record at tiny size "
                 "before any scale-up.\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log.info("wrote %s", path)


def main(*, calibrate: bool = False) -> dict[str, Any]:
    stats = asyncio.run(run_replay())
    write_baseline(stats)
    if calibrate:
        write_calibration(stats)
    return stats
