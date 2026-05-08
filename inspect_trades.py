"""Inspect trades from the most recent backtest_results.json.

Re-runs the detector on the historical bars at each signal date so we can show
which of the two setups fired and at what level, and walks the next 10 daily
bars annotated with where SL or TP1 actually printed.

Usage:
    py inspect_trades.py                            # default review:
                                                    #   SPY losses + MSFT losses + USOIL wins
    py inspect_trades.py --symbol NVDA              # all NVDA trades
    py inspect_trades.py --outcome win              # all winning trades, any symbol
    py inspect_trades.py --symbol MSFT --outcome loss
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

import httpx

import market_data
import smc_detector
from config import BACKTEST_RESULTS_FILE, assert_configured
from market_data import Bar

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass


@dataclass
class Trade:
    symbol: str
    timestamp: str
    direction: str
    score: int
    entry: float
    entry_zone_low: float
    entry_zone_high: float
    stop_loss: float
    take_profit_1: float
    rr_ratio: str
    outcome_5d: str
    outcome_10d: str

    @classmethod
    def from_dict(cls, sym: str, d: dict[str, Any]) -> "Trade":
        return cls(
            symbol=sym,
            timestamp=d["timestamp"],
            direction=d["direction"],
            score=int(d["score"]),
            entry=float(d["entry"]),
            entry_zone_low=float(d["entry_zone_low"]),
            entry_zone_high=float(d["entry_zone_high"]),
            stop_loss=float(d["stop_loss"]),
            take_profit_1=float(d["take_profit_1"]),
            rr_ratio=str(d.get("rr_ratio", "?")),
            outcome_5d=str(d["outcomes"]["5"]),
            outcome_10d=str(d["outcomes"]["10"]),
        )


def _find_bar_by_date(bars: list[Bar], iso_date: str) -> int:
    for i, b in enumerate(bars):
        if b.dt.date().isoformat() == iso_date:
            return i
    return -1


def _annotated_walk(direction: str, future: list[Bar], zone_low: float, zone_high: float,
                    sl: float, tp1: float) -> list[tuple[Bar, str]]:
    """Walk forward N bars, annotating where the entry/SL/TP1 first triggered."""
    triggered = False
    annotated: list[tuple[Bar, str]] = []
    resolved = False
    for b in future:
        notes: list[str] = []
        if not triggered:
            if b.l <= zone_high and b.h >= zone_low:
                triggered = True
                notes.append(f"entry-zone touched ({zone_low:.2f}-{zone_high:.2f})")
        if triggered and not resolved:
            if direction == "long":
                hit_sl = b.l <= sl
                hit_tp = b.h >= tp1
            else:
                hit_sl = b.h >= sl
                hit_tp = b.l <= tp1
            if hit_sl and hit_tp:
                notes.append(f"both SL ({sl:.2f}) and TP1 ({tp1:.2f}) hit — counted as LOSS")
                resolved = True
            elif hit_sl:
                notes.append(f"SL hit @ {sl:.2f}  → LOSS")
                resolved = True
            elif hit_tp:
                notes.append(f"TP1 hit @ {tp1:.2f}  → WIN")
                resolved = True
        annotated.append((b, "  " + " · ".join(notes) if notes else ""))
    return annotated


def _render_trade(trade: Trade, bars: list[Bar]) -> str:
    idx = _find_bar_by_date(bars, trade.timestamp)
    if idx < 0:
        return f"  {trade.symbol} {trade.timestamp}  [signal bar not found in current data]"

    history = bars[: idx + 1]
    score, direction, signals = smc_detector.score_setups(history)

    out: list[str] = []
    out.append(f"  {trade.symbol} {trade.timestamp}  {trade.direction.upper()}  score {trade.score}")
    out.append(f"    Outcomes:    5-day {trade.outcome_5d.upper():<11}  10-day {trade.outcome_10d.upper()}")
    out.append(f"    Re-detected: score {score}  direction {direction}")

    ob = signals.get("ob_retest")
    bos = signals.get("bos_retest")
    if ob:
        out.append(
            f"    OB retest:   {ob['direction']}  impulse {ob['impulse_pct'] * 100:.1f}%  "
            f"zone {ob['ob_low']:.2f}-{ob['ob_high']:.2f}  "
            f"(impulse bars {ob['impulse_start']}-{ob['impulse_end']}, OB bar {ob['ob_index']})"
        )
    else:
        out.append(f"    OB retest:   none")
    if bos:
        out.append(
            f"    BOS retest:  {bos['direction']}  level {bos['level']:.2f}  "
            f"(swing bar {bos['swing_index']}, broken at bar {bos['broken_at']})"
        )
    else:
        out.append(f"    BOS retest:  none")

    out.append(
        f"    Entry: {trade.entry:.2f}  zone {trade.entry_zone_low:.2f}-{trade.entry_zone_high:.2f}  "
        f"SL {trade.stop_loss:.2f}  TP1 {trade.take_profit_1:.2f}  R:R {trade.rr_ratio}"
    )
    out.append(f"    Forward 10 bars (date  open  high  low  close):")
    future = bars[idx + 1: idx + 11]
    if not future:
        out.append("      (no forward bars in current data)")
        return "\n".join(out)

    walk = _annotated_walk(trade.direction, future, trade.entry_zone_low,
                           trade.entry_zone_high, trade.stop_loss, trade.take_profit_1)
    for offset, (b, note) in enumerate(walk, start=1):
        date = b.dt.date().isoformat()
        out.append(f"      +{offset:>2}  {date}  O={b.o:>8.2f}  H={b.h:>8.2f}  L={b.l:>8.2f}  C={b.c:>8.2f}{note}")

    return "\n".join(out)


def _flatten_trades(data: dict[str, Any]) -> list[Trade]:
    out: list[Trade] = []
    for sym_block in data.get("per_symbol", []):
        sym = sym_block["symbol"]
        for fired in sym_block.get("fired", []):
            out.append(Trade.from_dict(sym, fired))
    return out


def _select_groups(args: argparse.Namespace, trades: list[Trade]) -> list[tuple[str, list[Trade]]]:
    if not args.symbol and not args.outcome:
        return [
            ("SPY  losing trades  (10-day)",
             [t for t in trades if t.symbol == "SPY" and t.outcome_10d == "loss"]),
            ("MSFT losing trades  (10-day)",
             [t for t in trades if t.symbol == "MSFT" and t.outcome_10d == "loss"]),
            ("USOIL winning trades (10-day)",
             [t for t in trades if t.symbol == "USOIL" and t.outcome_10d == "win"]),
        ]
    filtered = trades
    if args.symbol:
        filtered = [t for t in filtered if t.symbol == args.symbol.upper()]
    if args.outcome:
        filtered = [t for t in filtered if t.outcome_10d == args.outcome]
    label = f"trades"
    if args.symbol:
        label = f"{args.symbol.upper()} {label}"
    if args.outcome:
        label = f"{label} ({args.outcome})"
    return [(label, filtered)]


async def _fetch_symbols(symbols: set[str]) -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = {}
    async with httpx.AsyncClient(timeout=20.0) as client:
        for sym in sorted(symbols):
            print(f"  fetching {sym}…")
            out[sym] = await market_data.fetch_daily(client, sym)
    return out


async def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect signals from backtest_results.json.")
    parser.add_argument("--symbol", help="Filter to one symbol (e.g. NVDA)")
    parser.add_argument("--outcome", choices=["win", "loss", "open", "untriggered"],
                        help="Filter by 10-day outcome")
    args = parser.parse_args()

    assert_configured()

    if not BACKTEST_RESULTS_FILE.exists():
        print(f"ERROR: {BACKTEST_RESULTS_FILE.name} doesn't exist. Run `py scan.py --backtest` first.")
        return

    with BACKTEST_RESULTS_FILE.open(encoding="utf-8") as f:
        data = json.load(f)

    trades = _flatten_trades(data)
    groups = _select_groups(args, trades)

    needed = {t.symbol for _, ts in groups for t in ts}
    if not needed:
        print("No matching trades.")
        return

    print(f"Loading current daily bars for: {', '.join(sorted(needed))}")
    bars_map = await _fetch_symbols(needed)
    print()

    bar = "═" * 78
    for label, ts in groups:
        print(bar)
        print(f"  {label}: {len(ts)} trade(s)")
        print(bar)
        if not ts:
            print("  (none)")
            print()
            continue
        for t in ts:
            print()
            print(_render_trade(t, bars_map[t.symbol]))
        print()


if __name__ == "__main__":
    asyncio.run(main())
