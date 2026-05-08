"""Daily-only watcher for a single symbol.

    py watch.py --symbol SPY

Re-runs the daily SMC analysis on a configurable interval (default 4 hours
since Massive's free tier only refreshes daily candles after market close).
Fires a full alert to the dashboard the first time a setup goes from
"no trade" to "take trade".

Stop with Ctrl+C.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

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
    ANALYSIS_MIN_SCORE,
    LOG_FILE,
    SIGNAL_DEDUP_BARS,
    WATCH_INTERVAL_SECONDS,
    WATCHLIST,
    assert_configured,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("watch")


async def one_iteration(
    client: httpx.AsyncClient,
    symbol: str,
) -> tuple[dict[str, Any], int, int]:
    """Returns (brief, score, current_bar_idx). bar_idx = -1 on failure."""
    bars = await market_data.fetch_daily(client, symbol)
    if not bars:
        log.warning("%s — no daily candles returned", symbol)
        return ({
            "symbol": symbol, "confluence_score": 0, "direction": None,
            "htf_bias": "no_data", "patterns_detected": [],
            "reasoning": "Insufficient candle data this iteration.",
            "warnings": [], "take_trade": False,
        }, 0, -1)

    score, direction, signals = smc_detector.score_setups(bars)
    bias = smc_detector.simple_bias(bars)
    atr14 = smc_detector.atr(bars)

    intraday_task = asyncio.create_task(market_data.fetch_yf_hourly(symbol))
    news_task = asyncio.create_task(news_sentiment.fetch_news(symbol, limit=10))
    live_price_task = asyncio.create_task(market_data.fetch_live_price(symbol))
    enrichment = await enricher.enrich(client, symbol)
    h1_bars = await intraday_task
    news_items = await news_task
    live_price = await live_price_task
    h4_bars = market_data.build_synthetic_4h(h1_bars) if h1_bars else []

    if live_price is None:
        live_price = bars[-1].c

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
    return brief, adjusted_score, len(bars) - 1


async def run(symbol: str) -> None:
    if symbol not in WATCHLIST:
        log.warning("%s is not in WATCHLIST; will try the literal ticker.", symbol)

    interval_min = WATCH_INTERVAL_SECONDS // 60
    print(f"\nWatching {symbol} — re-checking every {interval_min} minutes (~{interval_min // 60}h). Ctrl+C to stop.\n")
    print("Note: Massive free tier serves EOD daily candles only — the chart only meaningfully")
    print("changes once per trading day after the close.\n")

    await dashboard.push_watching(symbol, "started")

    prev_score = -1
    recent_fires: list[dict[str, Any]] = []
    stop_event = asyncio.Event()

    def _stop(*_: Any) -> None:
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
    except (AttributeError, ValueError):
        pass

    async with httpx.AsyncClient(timeout=20.0) as client:
        while not stop_event.is_set():
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            try:
                brief, score, current_idx = await one_iteration(client, symbol)
            except Exception as e:
                log.exception("Iteration failed: %s", e)
                print(f"[{now_iso}] iteration failed: {e}")
            else:
                fired = False
                if brief.get("take_trade") and current_idx >= 0:
                    new_sig = {
                        "direction": brief["direction"],
                        "entry_zone_low": brief["entry_zone_low"],
                        "entry_zone_high": brief["entry_zone_high"],
                        "stop_loss": brief["stop_loss"],
                        "bar_idx": current_idx,
                    }
                    if not smc_detector.is_duplicate_signal(
                        new_sig, recent_fires,
                        current_idx=current_idx, bars_window=SIGNAL_DEDUP_BARS,
                    ):
                        recent_fires.append(new_sig)
                        if len(recent_fires) > 20:
                            recent_fires = recent_fires[-20:]
                        trade = memory.add_trade(brief)
                        await dashboard.push_alert(trade)
                        print(analyser.render_brief(trade))
                        fired = True

                if not fired:
                    if score > prev_score and score >= ANALYSIS_MIN_SCORE:
                        print(f"[{now_iso}] {symbol} — score {score} (already alerted within last {SIGNAL_DEDUP_BARS} bars)")
                    else:
                        direction = (brief.get("direction") or "—").upper()
                        print(f"[{now_iso}] {symbol} — score {score} dir {direction}: no trigger yet — next check in ~{interval_min // 60}h")

                prev_score = score
                await dashboard.push_watching(symbol, "scanning")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=WATCH_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    print("\nStopping…")
    await dashboard.push_watching(symbol, "stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily-only SMC watcher for a single symbol.")
    parser.add_argument("--symbol", required=True, help="Symbol to watch (e.g. SPY, NVDA, USOIL)")
    args = parser.parse_args()
    assert_configured()
    try:
        asyncio.run(run(args.symbol.upper()))
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
