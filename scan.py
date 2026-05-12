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
import chart_capture
import cooling_off
import dashboard
import enricher
import market_data
import memory
import news_sentiment
import paper_trader
import risk_engine
import smc_detector
from config import (
    BACKTEST_FROM_DATE,
    BACKTEST_HORIZONS,
    BACKTEST_RESULTS_FILE,
    BACKTEST_WARMUP_BARS,
    LOG_FILE,
    OB_IMPULSE_OVERRIDES,
    SCAN_MIN_SCORE,
    SCAN_RESULTS_FILE,
    SIGNAL_DEDUP_BARS,
    TRADINGVIEW_SYMBOLS,
    WATCHLIST,
    assert_configured,
)
from smc_detector import OB_IMPULSE_THRESHOLD
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

    threshold = OB_IMPULSE_OVERRIDES.get(symbol, OB_IMPULSE_THRESHOLD)
    score, direction, signals = smc_detector.score_setups(bars, impulse_threshold=threshold)
    bias = smc_detector.simple_bias(bars)
    atr14 = smc_detector.atr(bars)

    # Massive enrichment (different API) runs in parallel with the yfinance
    # block. Inside the yfinance block we go strictly sequential so the
    # 0.5s throttle in market_data.yf_throttle() can space requests out and
    # avoid Yahoo's silent rate limiting.
    enrichment_task = asyncio.create_task(enricher.enrich(client, symbol))
    h1_bars = await market_data.fetch_yf_hourly(symbol)
    live_price = await market_data.fetch_live_price(symbol)
    news_items = await news_sentiment.fetch_news(symbol, limit=10)
    enrichment = await enrichment_task
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

    # Adaptive confidence based on the paper-trading track record. Runs
    # after build_brief so the brief's base confidence is already set, and
    # only the live path uses it — backtest stays deterministic via the
    # skip_risk_engine flag on build_brief.
    try:
        adjusted_conf, risk_warnings = risk_engine.adjust_confidence(
            symbol=symbol,
            base_score=brief.get("confluence_score", 0),
            signals_detected=brief.get("patterns_detected") or [],
            news_sentiment=brief.get("news_sentiment"),
        )
        brief["confidence"] = adjusted_conf
        if risk_warnings:
            brief["warnings"] = list(brief.get("warnings") or []) + risk_warnings
    except Exception as e:
        log.warning("risk_engine.adjust_confidence failed for %s: %s", symbol, e)
    return brief


async def live_main() -> None:
    started = datetime.now(timezone.utc)
    print(f"\nScanning markets — {started.isoformat()}\n")

    # Refresh cooling-off state before scanning so build_brief sees the
    # latest blacklist. Bootstrap runs once (idempotent); evaluate adds new
    # auto-blacklisted symbols based on the live trade log.
    seeded = cooling_off.bootstrap()
    if seeded:
        print(f"Cooling-off bootstrap seeded: {', '.join(seeded)}")
    auto_added = cooling_off.evaluate(memory.list_trades())
    if auto_added:
        print(f"Cooling-off auto-added: {', '.join(auto_added)}")
    active_cool = cooling_off.current_state()
    if active_cool:
        print(f"Cooling-off active ({len(active_cool)}): {', '.join(sorted(active_cool))}")

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

    # Live prices collected during the scan loop — used for both the
    # paper-trader resolution pass and the dashboard's open-trade P&L.
    current_prices: dict[str, float] = {
        r["symbol"]: r["current_price"]
        for r in results
        if r.get("symbol") and r.get("current_price") is not None
    }

    # Resolve any open paper trades against the latest snapshot before
    # rendering, so the briefing and the paper-trading summary at the end
    # of the run reflect the same state. This is the bot's self-learning
    # loop — every scan it adjudicates the previous batch of bets.
    newly_closed: list[dict[str, Any]] = []
    try:
        newly_closed = paper_trader.check_open_trades(current_prices)
    except Exception as e:
        log.warning("paper_trader.check_open_trades failed: %s", e)
    if newly_closed:
        print(f"\nResolved {len(newly_closed)} paper trade(s):")
        for t in newly_closed:
            print(
                f"  {t['symbol']:<7} {t['direction']:<5} {t['outcome']:<10} "
                f"{t.get('pnl_r', 0):+.2f}R @ {t.get('close_price')}"
            )

    payload = {"timestamp": started.isoformat(), "results": results}
    tmp = SCAN_RESULTS_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    tmp.replace(SCAN_RESULTS_FILE)

    print()
    print(analyser.render_daily_briefing(results))

    # Only the top 3 by score are eligible to fire a live alert. With a 20-symbol
    # universe we want to ration attention — anything below the top 3 might still
    # be take_trade but is suppressed at the alert layer.
    sorted_by_score = sorted(results, key=lambda r: r.get("confluence_score", 0) or 0, reverse=True)
    top3 = sorted_by_score[:3]
    watch_candidates = [r for r in top3 if r.get("take_trade")]
    fired_candidates: list[dict[str, Any]] = []
    if watch_candidates:
        print()
        for r in watch_candidates:
            symbol = r["symbol"]
            direction = r.get("direction")
            entry_low = r.get("entry_zone_low")
            entry_high = r.get("entry_zone_high")
            sl = r.get("stop_loss")

            # Suppress identical re-fires inside the 6h dedup window. A signal
            # only counts as new when the entry zone has drifted past the
            # SIGNAL_DEDUP_ZONE_PCT tolerance — i.e. price has moved.
            if (
                direction
                and entry_low is not None
                and entry_high is not None
                and memory.check_recent_signal(symbol, direction, entry_low, entry_high)
            ):
                print(f"{symbol}: identical signal fired recently — skipping to avoid repeat alert")
                continue

            memory.add_trade(r)
            if (
                direction
                and entry_low is not None
                and entry_high is not None
                and sl is not None
            ):
                memory.log_fired_signal(
                    symbol, direction, entry_low, entry_high, sl, payload["timestamp"]
                )

            # Automatically open a paper trade so the bot builds its own
            # track record. risk_engine reads from this on the next scan.
            entry_price = r.get("entry")
            tp1 = r.get("take_profit_1")
            tp2 = r.get("take_profit_2")
            if (
                direction
                and entry_price is not None
                and sl is not None
                and tp1 is not None
                and tp2 is not None
            ):
                try:
                    paper_trader.open_paper_trade(
                        symbol=symbol,
                        direction=direction,
                        entry_price=entry_price,
                        sl=sl,
                        tp1=tp1,
                        tp2=tp2,
                        signal_score=r.get("confluence_score") or 0,
                        signals_detected=r.get("patterns_detected") or [],
                        news_sentiment=r.get("news_sentiment"),
                        regime=r.get("htf_bias"),
                        timestamp=payload["timestamp"],
                    )
                except Exception as e:
                    log.warning("paper_trader.open_paper_trade failed for %s: %s", symbol, e)

            fired_candidates.append(r)
            # Machine-readable marker for CI / cron pipelines to detect fires.
            print(f"TAKE TRADE: {symbol} {(direction or '?').upper()}")

    # Visual confirmation: capture Yahoo charts for any symbol at the alert-watch
    # threshold (looser than take_trade so we get screenshots for setups we want
    # to manually review, not only the ones that auto-fire). Charts are uploaded
    # as a separate artifact by the workflow.
    chart_targets = [
        r for r in results
        if (r.get("confluence_score") or 0) >= chart_capture.CHART_CAPTURE_MIN_SCORE
    ]
    captured_any = False
    if chart_targets:
        print(f"\nCapturing charts for {len(chart_targets)} symbol(s) "
              f"at score ≥ {chart_capture.CHART_CAPTURE_MIN_SCORE}…")
        for r in chart_targets:
            symbol = r["symbol"]
            tv_symbol = TRADINGVIEW_SYMBOLS.get(symbol, symbol)
            paths = await chart_capture.capture_charts(symbol, tv_symbol)
            if paths:
                captured_any = True
                print(f"  {symbol} ({r.get('confluence_score')}): {len(paths)} chart(s)")
                for p in paths:
                    print(f"    {p}")
            else:
                print(f"  {symbol}: chart capture returned no files")
        if captured_any:
            print("\nChart screenshots attached as workflow artifacts — "
                  "open Actions run to view")

    await dashboard.push_scan_complete(payload["timestamp"], results)
    print(f"\nDone. Results saved to {SCAN_RESULTS_FILE.name}.")
    if fired_candidates:
        symbols = ", ".join(r["symbol"] for r in fired_candidates)
        print(f"To live-watch: py watch.py --symbol <one of: {symbols}>")

    _print_paper_trading_summary(current_prices, newly_closed)


def _print_paper_trading_summary(
    current_prices: dict[str, float],
    newly_closed: list[dict[str, Any]],
) -> None:
    """End-of-scan summary of the self-built track record. Always prints,
    even with no trades, so the user can confirm the system is wired."""
    open_trades = paper_trader.list_open()
    stats = paper_trader.get_system_stats()
    bar = "─" * 60
    print()
    print(bar)
    print("  PAPER TRADING SUMMARY")
    print(bar)
    print(f"  Open trades:       {len(open_trades)}")
    print(f"  Closed trades:     {stats['total_trades']}")
    if stats["total_trades"]:
        wr = stats["win_rate"]
        print(f"  Win rate:          {wr * 100:.1f}%" if wr is not None else "  Win rate:          n/a")
        pf = stats["profit_factor"]
        if pf is None:
            pf_str = "n/a"
        elif pf == float("inf"):
            pf_str = "∞ (no losses yet)"
        else:
            pf_str = f"{pf:.2f}"
        print(f"  Profit factor:     {pf_str}")
        print(f"  Total R:           {stats['total_r']:+.2f}R")
        if stats["current_win_streak"]:
            print(f"  Current streak:    {stats['current_win_streak']} wins")
        elif stats["current_loss_streak"]:
            print(f"  Current streak:    {stats['current_loss_streak']} losses")
        if stats.get("best_symbol"):
            print(f"  Best symbol:       {stats['best_symbol']}")
        if stats.get("worst_symbol"):
            print(f"  Worst symbol:      {stats['worst_symbol']}")
    if newly_closed:
        print(f"  Resolved this run: {len(newly_closed)}")
    if open_trades:
        print()
        print(f"  Open positions ({len(open_trades)}):")
        annotated = paper_trader.compute_unrealised(open_trades, current_prices)
        for t in annotated:
            cp = t.get("current_price")
            r_now = t.get("unrealised_r")
            if cp is not None and r_now is not None:
                print(
                    f"    {t['symbol']:<7} {t['direction']:<5} entry {t['entry_price']:<10} "
                    f"live {cp:<10} ({r_now:+.2f}R)"
                )
            else:
                print(
                    f"    {t['symbol']:<7} {t['direction']:<5} entry {t['entry_price']:<10} "
                    "(no live price)"
                )
    print(bar)


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
    """Walk-forward backtest on a single symbol's daily series.

    Only signals firing on or after BACKTEST_FROM_DATE are recorded — earlier
    bars still feed the detector as context (warmup, swing history, OB origins)
    so the simulation reflects what would actually have been seen on those
    dates. The cut-off keeps the read focused on the current market regime."""
    from datetime import date as _date

    horizons = BACKTEST_HORIZONS
    fired: list[dict[str, Any]] = []
    cut_off = _date.fromisoformat(BACKTEST_FROM_DATE)

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
    skipped_pre_cutoff = 0
    recent_fires: list[dict[str, Any]] = []

    threshold = OB_IMPULSE_OVERRIDES.get(symbol, OB_IMPULSE_THRESHOLD)
    for i in range(BACKTEST_WARMUP_BARS, last_index + 1):
        # Backtest signal-fire cut-off — pre-2026 bars still feed history.
        if bars[i].dt.date() < cut_off:
            skipped_pre_cutoff += 1
            continue

        history = bars[:i + 1]
        score, direction, signals = smc_detector.score_setups(history, impulse_threshold=threshold)
        bias = smc_detector.simple_bias(history)
        atr14 = smc_detector.atr(history)

        days_simulated += 1
        brief = analyser.build_brief(
            symbol=symbol, score=score, direction=direction, signals=signals,
            current_price=round(history[-1].c, 5), bias=bias, atr14=atr14,
            enrichment={"headlines": [], "upcoming_events": []},
            skip_cooling_off=True,  # backtest must not be gated by live blacklist
            skip_risk_engine=True,  # backtest must not be biased by forward paper data
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
        "skipped_pre_cutoff": skipped_pre_cutoff,
        "cutoff_date": BACKTEST_FROM_DATE,
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
    print(f"Warmup: {BACKTEST_WARMUP_BARS} bars   Horizons: {BACKTEST_HORIZONS}")
    print(f"Signal-fire cut-off: {BACKTEST_FROM_DATE} (earlier bars feed context only)\n")

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
