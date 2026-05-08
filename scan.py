"""Daily SMC scan + backtest.

Usage:
    py scan.py                # one-shot live daily scan
    py scan.py --backtest     # walk-forward backtest on the same data

Live mode walks the watchlist, fetches daily candles, runs SMC detection,
calculates a confluence score, prints a daily briefing and writes
`scan_results.json`. Pushes to the dashboard if it's running.

Backtest mode pulls the same daily candles and walks the series day-by-day from
day BACKTEST_WARMUP_BARS onwards, treating each step as "today" and seeing what
signal would have fired and whether the next 5 / 10 trading days resolved as a
TP1-before-SL win, an SL-before-TP1 loss, an untriggered fill, or still open.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

# Ensure box-drawing characters render on Windows terminals (cp1252 default).
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass

import analyser
import dashboard
import enricher
import market_data
import memory
import news_sentiment
import smc_detector
from config import (
    BACKTEST_HORIZONS,
    BACKTEST_RESULTS_FILE,
    BACKTEST_WARMUP_BARS,
    LOG_FILE,
    SCAN_MIN_SCORE,
    SCAN_RESULTS_FILE,
    SIGNAL_DEDUP_BARS,
    WATCHLIST,
    assert_configured,
)
from market_data import Bar

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("scan")


# ---------- Live scan ----------

async def scan_symbol(client: httpx.AsyncClient, symbol: str) -> dict[str, Any]:
    """Two-setup detector across daily (Massive) + 1H/4H intraday (yfinance).

    Daily remains the take-trade gate. Intraday is informational confluence —
    surfaced in the brief so you can see whether 4H/1H structure agrees
    with the daily setup.
    """
    bars = await market_data.fetch_daily(client, symbol)
    if not bars:
        log.warning("Skipping %s — no daily candles returned", symbol)
        return {
            "symbol": symbol, "confluence_score": 0, "direction": None,
            "htf_bias": "no_data", "patterns_detected": [],
            "reasoning": "Insufficient data from Massive.",
            "warnings": [], "take_trade": False,
        }

    score, direction, signals = smc_detector.score_setups(bars)
    bias = smc_detector.simple_bias(bars)
    atr14 = smc_detector.atr(bars)

    # Intraday + yfinance news + live price all run concurrently with the
    # Massive news fetch.
    intraday_task = asyncio.create_task(market_data.fetch_yf_hourly(symbol))
    news_task = asyncio.create_task(news_sentiment.fetch_news(symbol, limit=10))
    live_price_task = asyncio.create_task(market_data.fetch_live_price(symbol))
    enrichment = await enricher.enrich(client, symbol)
    h1_bars = await intraday_task
    news_items = await news_task
    live_price = await live_price_task
    h4_bars = market_data.build_synthetic_4h(h1_bars) if h1_bars else []

    # Fall back to the Massive daily close (same instrument scale as the OB zones)
    # if live yfinance lookup fails. Don't fall back to 1H close — for USOIL the
    # 1H bars come from CL=F which is on a different price scale than USO daily.
    if live_price is None:
        live_price = bars[-1].c

    # Invalidate signals where intraday has already left the OB/BOS zone behind.
    signals, stale_reasons = smc_detector.invalidate_by_price(signals, live_price)
    score, direction = smc_detector.score_from_signals(signals)

    intraday: dict[str, Any] = {
        "h1_bars": len(h1_bars),
        "h4_bars": len(h4_bars),
        "h1": None,
        "h4": None,
    }
    if h4_bars:
        h4_score, h4_dir, h4_signals = smc_detector.score_setups(h4_bars)
        intraday["h4"] = {
            "score": h4_score, "direction": h4_dir, "signals": h4_signals,
            "current_price": round(h4_bars[-1].c, 5),
        }
    if h1_bars:
        h1_score, h1_dir, h1_signals = smc_detector.score_setups(h1_bars)
        intraday["h1"] = {
            "score": h1_score, "direction": h1_dir, "signals": h1_signals,
            "current_price": round(h1_bars[-1].c, 5),
        }

    news_analysis = news_sentiment.analyse_headlines(news_items)
    adjusted_score, news_warnings = smc_detector.apply_news_sentiment(
        score, direction, news_analysis,
    )
    macro_warn = news_sentiment.macro_warning(symbol)

    brief = analyser.build_brief(
        symbol=symbol, score=adjusted_score, direction=direction, signals=signals,
        current_price=round(live_price, 5), bias=bias, atr14=atr14,
        enrichment=enrichment, intraday=intraday,
        news_sentiment=news_analysis, news_warnings=news_warnings,
        macro_warning=macro_warn,
        staleness_reasons=stale_reasons,
    )
    return brief


async def live_main() -> None:
    started = datetime.now(timezone.utc)
    print(f"\nScanning markets — {started.isoformat()}\n")

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for symbol in WATCHLIST:
            try:
                brief = await scan_symbol(client, symbol)
            except Exception as e:
                log.exception("Scan failed for %s: %s", symbol, e)
                brief = {
                    "symbol": symbol, "confluence_score": 0, "direction": None,
                    "htf_bias": "error", "patterns_detected": [],
                    "reasoning": f"Scan error: {e}",
                    "warnings": [], "take_trade": False,
                }
            results.append(brief)
            score = brief.get("confluence_score", 0)
            direction = (brief.get("direction") or "—").upper()
            print(f"  {symbol:<7}  score {score:>3}  dir {direction:<5}  bias {brief.get('htf_bias', '?')}")

    payload = {"timestamp": started.isoformat(), "results": results}
    tmp = SCAN_RESULTS_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    tmp.replace(SCAN_RESULTS_FILE)

    print()
    print(analyser.render_daily_briefing(results))

    watch_candidates = [r for r in results if r.get("take_trade")]
    if watch_candidates:
        print()
        for r in watch_candidates:
            memory.add_trade(r)

    await dashboard.push_scan_complete(payload["timestamp"], results)
    print(f"\nDone. Results saved to {SCAN_RESULTS_FILE.name}.")
    if watch_candidates:
        symbols = ", ".join(r["symbol"] for r in watch_candidates)
        print(f"To live-watch: py watch.py --symbol <one of: {symbols}>")


# ---------- Backtest ----------

def _check_outcome(
    future_bars: list[Bar],
    direction: str,
    entry_zone_low: float,
    entry_zone_high: float,
    sl: float,
    tp1: float,
) -> str:
    """Walk forward bar-by-bar. Return 'win', 'loss', 'untriggered', or 'open'.

    A bar 'fills' the trade if its high/low range overlaps the entry zone.
    Once filled, the first of TP1 or SL hit (using the bar's high/low) wins.
    If both happen the same bar, we conservatively call it a loss (slippage
    on the SL side, since we can't tell intra-bar order on daily candles).
    """
    triggered = False
    for bar in future_bars:
        if not triggered:
            if bar.l <= entry_zone_high and bar.h >= entry_zone_low:
                triggered = True
        if triggered:
            if direction == "long":
                hit_sl = bar.l <= sl
                hit_tp = bar.h >= tp1
                if hit_sl and hit_tp:
                    return "loss"
                if hit_sl:
                    return "loss"
                if hit_tp:
                    return "win"
            else:
                hit_sl = bar.h >= sl
                hit_tp = bar.l <= tp1
                if hit_sl and hit_tp:
                    return "loss"
                if hit_sl:
                    return "loss"
                if hit_tp:
                    return "win"
    return "open" if triggered else "untriggered"


def _backtest_one(symbol: str, bars: list[Bar]) -> dict[str, Any]:
    """Walk-forward backtest on a single symbol's daily series."""
    horizons = BACKTEST_HORIZONS
    fired: list[dict[str, Any]] = []

    if len(bars) < BACKTEST_WARMUP_BARS + max(horizons) + 1:
        log.warning("%s: only %d bars, need at least %d for backtest",
                    symbol, len(bars), BACKTEST_WARMUP_BARS + max(horizons) + 1)
        return {
            "symbol": symbol, "bars_total": len(bars), "fired": [],
            "by_horizon": {str(h): {"fired": 0, "triggered": 0, "wins": 0,
                                     "losses": 0, "open": 0, "win_rate": 0.0}
                           for h in horizons},
            "days_simulated": 0,
        }

    # Last index where we still have max(horizons) future bars to evaluate
    last_index = len(bars) - max(horizons) - 1
    days_simulated = 0
    suppressed_dupes = 0
    recent_fires: list[dict[str, Any]] = []

    for i in range(BACKTEST_WARMUP_BARS, last_index + 1):
        history = bars[:i + 1]
        score, direction, signals = smc_detector.score_setups(history)
        bias = smc_detector.simple_bias(history)
        atr14 = smc_detector.atr(history)

        days_simulated += 1
        brief = analyser.build_brief(
            symbol=symbol, score=score, direction=direction, signals=signals,
            current_price=round(history[-1].c, 5), bias=bias, atr14=atr14,
            enrichment={"headlines": [], "upcoming_events": []},
        )

        if not brief.get("take_trade"):
            continue

        new_sig = {
            "direction": brief["direction"],
            "entry_zone_low": brief["entry_zone_low"],
            "entry_zone_high": brief["entry_zone_high"],
            "stop_loss": brief["stop_loss"],
            "bar_idx": i,
        }
        if smc_detector.is_duplicate_signal(
            new_sig, recent_fires, current_idx=i, bars_window=SIGNAL_DEDUP_BARS
        ):
            suppressed_dupes += 1
            continue
        recent_fires.append(new_sig)
        if len(recent_fires) > 20:
            recent_fires = recent_fires[-20:]

        outcomes_at: dict[int, str] = {}
        for horizon in horizons:
            future = bars[i + 1:i + 1 + horizon]
            outcomes_at[horizon] = _check_outcome(
                future, direction,
                brief["entry_zone_low"], brief["entry_zone_high"],
                brief["stop_loss"], brief["take_profit_1"],
            )

        fired.append({
            "bar_index": i,
            "timestamp": bars[i].dt.date().isoformat(),
            "direction": direction,
            "score": score,
            "entry": brief["entry"],
            "entry_zone_low": brief["entry_zone_low"],
            "entry_zone_high": brief["entry_zone_high"],
            "stop_loss": brief["stop_loss"],
            "take_profit_1": brief["take_profit_1"],
            "rr_ratio": brief["rr_ratio"],
            "atr14": brief.get("atr14"),
            "sl_buffer_used": brief.get("sl_buffer_used"),
            "sl_buffer_source": brief.get("sl_buffer_source"),
            "outcomes": {str(h): outcomes_at[h] for h in horizons},
        })

    by_horizon: dict[str, dict[str, Any]] = {}
    for h in horizons:
        triggered = sum(1 for f in fired if f["outcomes"][str(h)] != "untriggered")
        wins = sum(1 for f in fired if f["outcomes"][str(h)] == "win")
        losses = sum(1 for f in fired if f["outcomes"][str(h)] == "loss")
        opens = sum(1 for f in fired if f["outcomes"][str(h)] == "open")
        win_rate = (wins / triggered * 100) if triggered else 0.0
        by_horizon[str(h)] = {
            "fired": len(fired),
            "triggered": triggered,
            "wins": wins,
            "losses": losses,
            "open": opens,
            "win_rate": round(win_rate, 1),
        }

    return {
        "symbol": symbol,
        "bars_total": len(bars),
        "days_simulated": days_simulated,
        "suppressed_duplicates": suppressed_dupes,
        "fired": fired,
        "by_horizon": by_horizon,
    }


def _render_backtest_report(per_symbol: list[dict[str, Any]]) -> str:
    bar = "═" * 70
    lines: list[str] = [bar, "  BACKTEST RESULTS — daily SMC, walk-forward", bar]

    total_fired = 0
    horizon_totals: dict[str, dict[str, int]] = {
        str(h): {"fired": 0, "triggered": 0, "wins": 0, "losses": 0, "open": 0}
        for h in BACKTEST_HORIZONS
    }

    for r in per_symbol:
        lines.append("")
        lines.append(f"  {r['symbol']}")
        lines.append(f"    bars total:      {r['bars_total']}")
        lines.append(f"    days simulated:  {r['days_simulated']}  (from day {BACKTEST_WARMUP_BARS} onwards)")
        signals_fired = r["by_horizon"][str(BACKTEST_HORIZONS[0])]["fired"]
        lines.append(f"    signals fired:   {signals_fired}  (deduped {r.get('suppressed_duplicates', 0)})")
        if signals_fired == 0:
            lines.append("      (no take-trade signals during the simulation window)")
            continue
        for h in BACKTEST_HORIZONS:
            d = r["by_horizon"][str(h)]
            lines.append(
                f"    {h:>2}-day:  triggered {d['triggered']:>3}/{d['fired']:<3}  "
                f"wins {d['wins']:>3}  losses {d['losses']:>3}  open {d['open']:>3}  "
                f"win-rate {d['win_rate']:>5}%  (of triggered)"
            )

        total_fired += signals_fired
        for h in BACKTEST_HORIZONS:
            d = r["by_horizon"][str(h)]
            t = horizon_totals[str(h)]
            t["fired"] += d["fired"]
            t["triggered"] += d["triggered"]
            t["wins"] += d["wins"]
            t["losses"] += d["losses"]
            t["open"] += d["open"]

    lines.append("")
    lines.append(bar)
    lines.append("  AGGREGATE")
    lines.append(bar)
    if total_fired == 0:
        lines.append("  No signals fired across any symbol — try lowering ANALYSIS_MIN_SCORE")
        lines.append("  or relaxing min_impulse_pct in find_order_blocks.")
    else:
        for h in BACKTEST_HORIZONS:
            t = horizon_totals[str(h)]
            trigger_rate = (t["triggered"] / t["fired"] * 100) if t["fired"] else 0
            win_rate = (t["wins"] / t["triggered"] * 100) if t["triggered"] else 0
            lines.append(
                f"  {h:>2}-day:  fired {t['fired']:>3}  "
                f"triggered {t['triggered']:>3} ({trigger_rate:>5.1f}%)  "
                f"wins {t['wins']:>3}  losses {t['losses']:>3}  open {t['open']:>3}  "
                f"win-rate {win_rate:>5.1f}%"
            )
    lines.append(bar)
    return "\n".join(lines)


async def backtest_main() -> None:
    started = datetime.now(timezone.utc)
    print(f"\nBacktesting — {started.isoformat()}\n")
    print(f"Warmup: {BACKTEST_WARMUP_BARS} bars   Horizons: {BACKTEST_HORIZONS}\n")

    per_symbol: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for symbol in WATCHLIST:
            print(f"  fetching {symbol}…")
            try:
                bars = await market_data.fetch_daily(client, symbol)
            except Exception as e:
                log.exception("Fetch failed for %s: %s", symbol, e)
                continue
            if not bars:
                log.warning("No bars for %s, skipping", symbol)
                continue
            print(f"    got {len(bars)} bars; running walk-forward…")
            result = _backtest_one(symbol, bars)
            per_symbol.append(result)

    print()
    print(_render_backtest_report(per_symbol))

    payload = {
        "timestamp": started.isoformat(),
        "warmup_bars": BACKTEST_WARMUP_BARS,
        "horizons": list(BACKTEST_HORIZONS),
        "per_symbol": per_symbol,
    }
    tmp = BACKTEST_RESULTS_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    tmp.replace(BACKTEST_RESULTS_FILE)
    print(f"\nFull results saved to {BACKTEST_RESULTS_FILE.name}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily SMC scan / backtest.")
    parser.add_argument("--backtest", action="store_true",
                        help="Walk-forward backtest on the same daily data instead of a live scan.")
    args = parser.parse_args()

    assert_configured()
    if args.backtest:
        asyncio.run(backtest_main())
    else:
        asyncio.run(live_main())


if __name__ == "__main__":
    main()
