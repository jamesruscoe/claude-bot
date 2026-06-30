"""In-sample seed backfill (walk-forward over the last N calendar days).

Replays the FX path EXACTLY as the live daily scan would have seen each day —
same detector, same `FX_MIN_SCORE` gate, same honest intrabar SL-first
resolution, same (raw, size_mult=1.0) sizing as the Phase-1 harness — and writes
the resulting trades to a SEPARATE ledger (`state/ledger_backfill.db`) tagged
`source="backfill"`. It never touches the live forward `ledger.db`, so the
in-sample / out-of-sample wall is unambiguous.

This is a SANITY CHECK / seed, **not** forward validation: every trade here was
chosen with hindsight over the same window it's measured on.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from config import BACKTEST_WARMUP_BARS
from v2 import config as cfg
from v2 import datasource, replay, signals, store

log = logging.getLogger(__name__)

_WIN, _LOSS = {store.OUTCOME_WIN_TP2}, {store.OUTCOME_LOSS}

# CALIBRATION.md reference for dual-confluence (the FX_MIN_SCORE>=85 set).
_TARGET_PER_WEEK = 0.68
_TARGET_AVG_R = 0.35


def _resolve_open_backfill(bars_by_symbol: dict[str, list]) -> None:
    """Resolve every open backfill trade against a per-trade EXPIRY-capped forward
    window (so a trade expires at the holding limit instead of resolving on a
    much-later bar — the cap the live intraday path gets for free)."""
    now = datetime.now(timezone.utc)
    for t in store.list_open_trades():
        bars = bars_by_symbol.get(t["symbol"], [])
        opened = datetime.fromisoformat(str(t["opened_at"]))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        forward = [b for b in bars if b.dt > opened
                   and store._trading_days_between(opened, b.dt) <= cfg.EXPIRY_TRADING_DAYS]
        wnow = forward[-1].dt if forward else now
        outcome, close = store.walk_trade(t, forward, wnow)
        if outcome is None:
            if t.get("_dirty"):
                store.update_trade_trailing(t["id"], t["tp1_hit"], t["tp1_hit_at"], t["stop_loss"])
            continue
        raw = round(store._pnl_r(t["entry_price"], t["original_sl"], close, t["direction"]), 3)
        sized = round(raw * float(t.get("size_mult") or 1.0), 3)
        store.update_trade_close(t["id"], outcome, close, wnow.isoformat(), sized, raw)


async def run_backfill(days: int = 30) -> dict:
    if not cfg.FX_ENABLED:
        print("Backfill is FX-only — run with BOT_MARKET=fx.")
        return {"skipped": True, "reason": "not fx"}

    cfg.ensure_state_dirs()
    # SEPARATE ledger — the live forward ledger.db is never opened here.
    cfg.DB_PATH = cfg.STATE_DIR / "ledger_backfill.db"
    if cfg.DB_PATH.exists():
        cfg.DB_PATH.unlink()  # fresh, reproducible seed each run
    store.init_db()

    src = datasource.get_data_source()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    bars_by_symbol: dict[str, list] = {}
    detected = gated = opened = 0

    for sym in src.symbols():
        bars = await src.fetch_daily(sym)
        if len(bars) < BACKTEST_WARMUP_BARS + 5:
            continue
        bars_by_symbol[sym] = bars
        inst = replay._instrument(sym)
        open_dirs: dict[str, int] = {}
        start = max(BACKTEST_WARMUP_BARS, 20)
        for i in range(start, len(bars)):
            if bars[i].dt < cutoff:
                continue  # only the last `days` calendar days are "scan days"
            cand, _ = signals.build_candidate(
                sym, bars[: i + 1], live_price=bars[i].c, instrument=inst,
                impulse_threshold=cfg.FX_OB_IMPULSE_THRESHOLD)
            if cand is None:
                continue
            detected += 1
            if cand["score"] < cfg.FX_MIN_SCORE:   # SAME live gate
                continue
            gated += 1
            d = cand["direction"]
            if open_dirs.get(d, -1) > i:           # dedup same symbol+direction
                continue
            store.open_trade(None, cand, cand["entry"], opened_at=bars[i].dt.isoformat(),
                             size="full", source="backfill")
            opened += 1
            open_dirs[d] = min(i + cfg.EXPIRY_TRADING_DAYS, len(bars))

    _resolve_open_backfill(bars_by_symbol)
    if hasattr(src, "aclose"):
        await src.aclose()

    return _summary(days, detected, gated, opened)


def _summary(days: int, detected: int, gated: int, opened: int) -> dict:
    trades = store.trades_by_source("backfill")
    resolved = [t for t in trades if t["outcome"] is not None]
    still_open = len(trades) - len(resolved)
    wins = sum(1 for t in resolved if t["outcome"] in _WIN)
    losses = sum(1 for t in resolved if t["outcome"] in _LOSS)
    dec = wins + losses
    rs = [t["raw_r"] for t in resolved if t["raw_r"] is not None]
    win_rate = (wins / dec) if dec else None
    avg_r = (sum(rs) / len(rs)) if rs else None
    weeks = days / 7.0
    per_week = opened / weeks if weeks else 0
    expected = _TARGET_PER_WEEK * weeks

    print("\n" + "=" * 64)
    print("  IN-SAMPLE BACKFILL — SANITY CHECK (NOT forward validation)")
    print("  Hindsight replay over the same window it is measured on.")
    print("  Separate ledger: %s · source='backfill'" % cfg.DB_PATH)
    print("=" * 64)
    print(f"  Window:           last {days} calendar days (~{weeks:.1f} weeks)")
    print(f"  Detector setups:  {detected} candidate(s) at score>=50")
    print(f"  Passed FX_MIN_SCORE={cfg.FX_MIN_SCORE}: {gated}")
    print(f"  Trades opened:    {opened}  ({still_open} still open, {len(resolved)} resolved)")
    wr = f"{win_rate*100:.0f}%" if win_rate is not None else "n/a"
    ar = f"{avg_r:+.2f}R" if avg_r is not None else "n/a"
    print(f"  Win rate:         {wr}  (over {dec} decided)")
    print(f"  Avg R (raw):      {ar}")
    if trades:
        print("\n  Trades:")
        print("  %-12s %-9s %-6s %-12s %8s" % ("date", "symbol", "dir", "outcome", "R"))
        for t in trades:
            d = str(t["opened_at"])[:10]
            r = f"{t['raw_r']:+.2f}" if t.get("raw_r") is not None else "—"
            print("  %-12s %-9s %-6s %-12s %8s" %
                  (d, t["symbol"], t["direction"], t["outcome"] or "OPEN", r))

    # Sanity assertion vs CALIBRATION.md (~0.68 dual-confluence/week, ~+0.35R).
    print("\n  Sanity vs CALIBRATION.md (~%.2f trades/wk, ~%+.2fR):" % (_TARGET_PER_WEEK, _TARGET_AVG_R))
    print(f"    cadence: {per_week:.2f}/wk  (expected ~{expected:.1f} trades this window)")
    print(f"    avg R:   {ar}")
    cadence_ok = opened <= max(3, expected * 3)          # not WILDLY more than expected
    r_ok = (avg_r is None) or (dec < 3) or (-0.5 <= avg_r <= 1.5)
    if cadence_ok and r_ok:
        verdict = ("CONSISTENT with CALIBRATION (small sample — in-sample, not a forward result)"
                   if dec >= 1 else "no resolved trades yet — too few to compare (consistent: dual-confluence is rare)")
        print(f"  => {verdict}")
        bug = False
    else:
        print("  => OUT OF BALLPARK — investigate the live FX path before trusting this. "
              f"(cadence_ok={cadence_ok}, r_ok={r_ok})")
        bug = True
    print("=" * 64 + "\n")

    return {"detected": detected, "gated": gated, "opened": opened,
            "resolved": len(resolved), "still_open": still_open,
            "win_rate": win_rate, "avg_r": avg_r, "per_week": per_week,
            "suspected_bug": bug, "db": str(cfg.DB_PATH)}


def main(days: int = 30) -> dict:
    return asyncio.run(run_backfill(days=days))
