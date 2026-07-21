"""Pipeline orchestrator.

One scan, end to end:

  gate → fetch → detect → levels → retrieve memory → judge → record
       → open (deduped) → resolve open trades → reflect → emit

Every step is logged and every decision is persisted to the ledger, so the
`state` branch is a complete, replayable history of what the bot saw and why
it acted. Nothing here fires on a closed market or a stale feed (the v1 bugs),
and a symbol you're already in won't re-alert (kills the duplicate emails).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from v2 import brain, datasource, fx_filters, journal, news_calendar, signals, store
from v2.calendar_gate import bars_are_fresh, is_trading_day
from v2.config import (
    FX_ACCOUNT_EQUITY,
    FX_ENABLED,
    FX_OB_IMPULSE_THRESHOLD,
    FX_RISK_PCT,
    FX_STD_LOT_UNITS,
    LLM_ENABLED,
    SCAN_OUTPUT_FILE,
    ensure_state_dirs,
    fx_pip_size,
    fx_spread_pips,
)

log = logging.getLogger(__name__)


def _attach_fx_context(candidate: dict[str, Any]) -> None:
    """Annotate an FX candidate with session / news / correlation context so the
    LLM judge can weigh them. The deterministic brain ignores these keys."""
    if not FX_ENABLED:
        return
    sym, direction = candidate["symbol"], candidate["direction"]
    _, session = fx_filters.session_ok(sym)
    _, news = news_calendar.news_blackout(sym)
    _, corr = fx_filters.correlation_cap_ok(sym, direction, store.list_open_trades())
    candidate["session"] = session
    candidate["news_proximity"] = news
    candidate["correlation"] = corr


def _fx_open_block(symbol: str, candidate: dict[str, Any]) -> str | None:
    """FX-only open-time gates (session / news / correlation). Returns a block
    reason, or None if the trade is clear to open. Can only ever block."""
    if not FX_ENABLED:
        return None
    from v2.config import FX_MIN_SCORE
    if candidate["score"] < FX_MIN_SCORE:
        return f"threshold:below calibrated FX_MIN_SCORE ({candidate['score']}<{FX_MIN_SCORE})"
    ok, why = fx_filters.session_ok(symbol)
    if not ok:
        return f"session:{why}"
    blocked, why = news_calendar.news_blackout(symbol)
    if blocked:
        return f"news:{why}"
    ok, why = fx_filters.correlation_cap_ok(
        symbol, candidate["direction"], store.list_open_trades())
    if not ok:
        return f"correlation:{why}"
    return None


def _instrument_for(symbol: str) -> signals.Instrument | None:
    """FX risk-math context for a symbol, or None for the equities path."""
    if not FX_ENABLED:
        return None
    return signals.Instrument(
        symbol=symbol, pip_size=fx_pip_size(symbol), spread_pips=fx_spread_pips(symbol),
        equity=FX_ACCOUNT_EQUITY, risk_pct=FX_RISK_PCT, std_lot=FX_STD_LOT_UNITS,
    )


# ---------- live scan -------------------------------------------------------

async def run_scan(*, force: bool = False) -> dict[str, Any]:
    """Run one live scan. `force` bypasses the market-open gate (manual runs)."""
    ensure_state_dirs()
    store.init_db()
    scan_ts = datetime.now(timezone.utc).isoformat()
    source = datasource.get_data_source()

    # FX trades through US holidays, so for FX we only gate on the weekend (the
    # staleness check below catches a feed that didn't update). Equities keep the
    # full NYSE holiday calendar.
    gate = is_trading_day()
    if not gate and not force:
        weekend = "weekend" in gate.reason
        if not FX_ENABLED or weekend:
            log.info("market closed — %s. Skipping scan.", gate.reason)
            return {"scan_ts": scan_ts, "skipped": True, "reason": gate.reason,
                    "candidates": [], "opened": [], "closed": []}

    bars_by_symbol: dict[str, list] = {}
    for symbol in source.symbols():
        bars = await source.fetch_daily(symbol)
        if bars:
            bars_by_symbol[symbol] = bars

    if not bars_by_symbol:
        log.warning("no data for any symbol — aborting scan (never act on an empty feed)")
        skip = {"scan_ts": scan_ts, "skipped": True, "reason": "no data",
                "candidates": [], "opened": [], "closed": []}
        from v2 import alerts  # a dead feed is a feed-health alert, not silence
        alerts.evaluate_and_write(skip)
        return skip

    prices = {s: b[-1].c for s, b in bars_by_symbol.items()}

    rows: list[dict[str, Any]] = []
    opened: list[dict[str, Any]] = []
    for symbol, bars in bars_by_symbol.items():
        fresh = bars_are_fresh(bars[-1].dt)
        if not fresh and not force:
            log.info("skip %s — %s", symbol, fresh.reason)
            store.record_rejection(scan_ts, symbol, "data", "stale_feed")
            continue

        candidate, reason = signals.build_candidate(
            symbol, bars, live_price=prices[symbol], instrument=_instrument_for(symbol),
            impulse_threshold=FX_OB_IMPULSE_THRESHOLD if FX_ENABLED else None)
        if candidate is None:
            store.record_rejection(scan_ts, symbol, "detector", reason or "unknown")
            rows.append({"symbol": symbol, "candidate": False, "reject_reason": reason})
            continue

        _attach_fx_context(candidate)
        retrieval = journal.retrieve_for(candidate)
        decision = brain.judge(candidate, retrieval)

        sig_id = store.record_signal(scan_ts, candidate)
        store.record_decision(sig_id, scan_ts, symbol, decision)

        row = {"symbol": symbol, "candidate": True, **_public(candidate), **{
            "take": decision["take"], "confidence": decision["confidence"],
            "size": decision["size"], "rationale": decision["rationale"],
            "decided_by": decision["source"]}}

        if not decision["take"]:
            store.record_rejection(scan_ts, symbol, "judge", "judge_skip",
                                   candidate["score"], candidate["direction"])
        elif not _should_open(symbol, candidate):
            pass  # already in this symbol+direction (logged in _should_open)
        elif (block := _fx_open_block(symbol, candidate)) is not None:
            stage, _, why = block.partition(":")
            log.info("skip open %s — %s", symbol, why)
            store.record_rejection(scan_ts, symbol, f"fx_{stage}", why,
                                   candidate["score"], candidate["direction"])
            row["fx_blocked"] = why
        else:
            fill = candidate["entry"]
            trade = store.open_trade(sig_id, candidate, fill, opened_at=scan_ts,
                                     size=decision["size"])
            opened.append(trade)
            row["opened"] = True
        rows.append(row)

    closed = await _resolve(source, bars_by_symbol)
    journaled = brain.reflect_on_closed(closed)
    if journaled:
        log.info("reflected on %d resolved trade(s)", journaled)

    if hasattr(source, "aclose"):
        await source.aclose()

    payload = {
        "scan_ts": scan_ts, "skipped": False, "llm": LLM_ENABLED, "market": source.name,
        "results": rows, "opened": opened, "closed": closed,
        "system_stats": store.system_stats(),
        "rejections": store.rejection_counts(since=scan_ts),
    }
    _emit(payload)
    return payload


async def _resolve(source: "datasource.DataSource",
                   daily_by_symbol: dict[str, list]) -> list[dict[str, Any]]:
    """Adjudicate open trades honestly. FX uses intraday bars (SL/TP-first);
    equities fall back to the daily bars already fetched this scan."""
    open_trades = store.list_open_trades()
    if not open_trades:
        return []
    res_bars: dict[str, list] = {}
    for t in open_trades:
        sym = t["symbol"]
        if sym in res_bars:
            continue
        if source.intraday_supported:
            try:
                opened = datetime.fromisoformat(str(t["opened_at"]))
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
            except ValueError:
                opened = datetime.now(timezone.utc)
            res_bars[sym] = await source.resolution_bars(sym, opened)
        else:
            res_bars[sym] = daily_by_symbol.get(sym, [])
    return store.resolve_open_trades(res_bars)


async def resolve_only() -> dict[str, Any]:
    """Adjudicate open trades against fresh data WITHOUT scanning for new ones.
    Backs the --resolve-only flag (previously dead — audit cleanup)."""
    ensure_state_dirs()
    store.init_db()
    source = datasource.get_data_source()
    closed = await _resolve(source, {})
    journaled = brain.reflect_on_closed(closed)
    if hasattr(source, "aclose"):
        await source.aclose()
    log.info("resolve-only: %d closed, %d journaled", len(closed), journaled)
    print(f"\nResolve-only — {len(closed)} trade(s) closed:")
    for t in closed:
        print(f"  {t['symbol']} {t['outcome']} ({t['pnl_r']}R)")
    return {"closed": closed, "system_stats": store.system_stats()}


def _public(candidate: dict[str, Any]) -> dict[str, Any]:
    keys = ("score", "direction", "setups", "regime", "price", "atr",
            "entry", "entry_low", "entry_high", "stop_loss", "tp1", "tp2", "rr")
    return {k: candidate.get(k) for k in keys}


def _should_open(symbol: str, candidate: dict[str, Any]) -> bool:
    """Dedup: don't open a second position in a symbol+direction we're already
    in. This is what stops the v1 'same signal emailed 3 times' problem — an
    open position means the alert already happened."""
    for t in store.list_open_trades():
        if t["symbol"] == symbol and t["direction"] == candidate["direction"]:
            log.info("skip open %s %s — already in an open position",
                     symbol, candidate["direction"])
            return False
    return True


def _emit(payload: dict[str, Any]) -> None:
    """Write the machine-readable scan file (dashboard/email) and print a human
    summary with TAKE-TRADE markers for any newly opened position."""
    tmp = SCAN_OUTPUT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(SCAN_OUTPUT_FILE)

    print(render_summary(payload))
    try:
        from v2 import report
        report.write_daily_report(payload)
    except Exception as e:  # never let reporting break a scan
        log.debug("daily report failed: %s", e)
    for t in payload["opened"]:
        # CI marker — the workflow greps this to decide whether to email.
        print(f"TAKE TRADE: {t['symbol']} {t['direction'].upper()} "
              f"@ {t['entry_price']} sl {t['stop_loss']} tp1 {t['tp1']} tp2 {t['tp2']}")

    # FX-only: format a rich signal / feed-health alert for the workflow's mail
    # step. Internally FX-gated and fail-open — never breaks a scan or the ledger.
    from v2 import alerts
    alerts.evaluate_and_write(payload)


def render_summary(payload: dict[str, Any]) -> str:
    if payload.get("skipped"):
        return f"\nScan skipped — {payload['reason']}\n"
    lines = [f"\nScan {payload['scan_ts']}  (judge: {'Claude' if payload['llm'] else 'deterministic'})", ""]
    cands = [r for r in payload["results"] if r.get("candidate")]
    cands.sort(key=lambda r: (r.get("take", False), r.get("score", 0)), reverse=True)
    if not cands:
        lines.append("  no candidates today")
    for r in cands:
        verdict = "TAKE" if r.get("take") else "skip"
        opened = "  ← opened" if r.get("opened") else ""
        lines.append(
            f"  {r['symbol']:<6} {r['direction'] or '—':<5} score {r['score']:>3} "
            f"R:R {r.get('rr')}  [{verdict} · {r['confidence']} · {r['size']}]{opened}")
        lines.append(f"         {r['rationale']}")
    st = payload["system_stats"]
    wr = f"{st['win_rate'] * 100:.0f}%" if st["win_rate"] is not None else "n/a"
    lines.append("")
    lines.append(f"  Ledger: {st['open']} open · {st['total_closed']} closed · "
                 f"win rate {wr} · {st['total_r']:+.2f}R")
    if payload["closed"]:
        lines.append(f"  Resolved this run: " + ", ".join(
            f"{t['symbol']} {t['outcome']} ({t['pnl_r']}R)" for t in payload["closed"]))
    return "\n".join(lines)


# ---------- self-test (no network, no API, temp state) ----------------------

def selftest() -> bool:
    """Exercise the whole deterministic loop offline so the rebuild can be
    proven without a market feed or an API key. Builds a synthetic uptrend +
    retest, runs it through detect → judge → open → resolve → reflect, and
    checks memory landed on disk."""
    import tempfile
    from pathlib import Path
    from market_data import Bar

    import v2.config as cfg
    tmp = Path(tempfile.mkdtemp(prefix="botv2_selftest_"))
    cfg.STATE_DIR = tmp
    cfg.DB_PATH = tmp / "ledger.db"
    cfg.JOURNAL_DIR = tmp / "journal"
    cfg.LESSONS_DIR = tmp / "lessons"
    cfg.SCAN_OUTPUT_FILE = tmp / "last_scan.json"
    store.init_db()

    # Synthetic daily series: long base, a 4% impulse up, then a pullback into
    # the order block — exactly what detect_ob_retest looks for.
    bars: list[Bar] = []
    t0 = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    day = 86_400_000
    price = 100.0
    for i in range(60):
        price += 0.05
        bars.append(Bar(t=t0 + i * day, o=price, h=price + 0.5, l=price - 0.5, c=price + 0.1, v=1e6))
    # opposite-colour OB candle (red), then a 3-bar bullish impulse
    ob = bars[-1].c
    bars.append(Bar(t=t0 + 60 * day, o=ob + 0.2, h=ob + 0.3, l=ob - 0.8, c=ob - 0.6, v=1e6))  # red OB
    base = bars[-1].c
    for i in range(3):
        base += 1.6
        bars.append(Bar(t=t0 + (61 + i) * day, o=base - 1.5, h=base + 0.3, l=base - 1.6, c=base, v=2e6))
    # pullback into the OB zone (current bar retests)
    bars.append(Bar(t=t0 + 64 * day, o=base, h=base, l=ob - 0.7, c=ob - 0.2, v=1e6))

    cand, reason = signals.build_candidate("TEST", bars, live_price=bars[-1].c)
    assert cand is not None, f"selftest: detector failed to produce a candidate ({reason})"
    retrieval = journal.retrieve_for(cand)
    decision = brain.judge(cand, retrieval)
    sig_id = store.record_signal("2026-01-05T00:00:00+00:00", cand)
    store.record_decision(sig_id, "2026-01-05T00:00:00+00:00", "TEST", decision)
    trade = store.open_trade(sig_id, cand, cand["entry"],
                             opened_at="2026-01-05T00:00:00+00:00", size="full")

    # Force a win: a later bar whose HIGH pierces TP2 (honest intrabar resolve).
    win_bar = Bar(t=t0 + 70 * day, o=cand["entry"], h=cand["tp2"] + 1,
                  l=cand["entry"], c=cand["tp2"] + 0.5, v=1e6)
    closed = store.resolve_open_trades({"TEST": [win_bar]})
    assert closed and closed[0]["outcome"] == "WIN_TP2", f"selftest: expected WIN_TP2, got {closed}"
    n = brain.reflect_on_closed(closed)
    assert n == 1, "selftest: reflection did not journal the trade"
    md = list((tmp / "journal").glob("*.md"))
    assert md, "selftest: no journal markdown written"

    print("SELFTEST OK")
    print(f"  candidate: {cand['symbol']} {cand['direction']} "
          f"entry {cand['entry']} sl {cand['stop_loss']} tp1 {cand['tp1']} tp2 {cand['tp2']} R:R {cand['rr']}")
    print(f"  decision : take={decision['take']} {decision['confidence']}/{decision['size']} "
          f"({decision['source']})")
    print(f"  resolved : {closed[0]['outcome']} {closed[0]['pnl_r']}R")
    print(f"  journal  : {md[0].name}")
    print(f"  state dir: {tmp}")
    return True
