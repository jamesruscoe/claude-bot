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


def _instrument(symbol: str) -> signals.Instrument | None:
    if not cfg.FX_ENABLED:
        return None
    return signals.Instrument(
        symbol=symbol, pip_size=cfg.fx_pip_size(symbol),
        spread_pips=cfg.fx_spread_pips(symbol),
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


def replay_symbol(symbol: str, bars: list[Bar]) -> dict[str, Any]:
    """Walk one symbol. Returns trades (resolved + still-open) and rejection
    reason counts."""
    inst = _instrument(symbol)
    trades: list[dict[str, Any]] = []
    rejections: dict[str, int] = {}
    open_dirs: dict[str, int] = {}  # direction -> index of bar it resolves by (dedup)

    n = len(bars)
    start = max(BACKTEST_WARMUP_BARS, 20)
    eval_days = max(0, n - start)
    for i in range(start, n):
        window = bars[: i + 1]
        cand, reason = signals.build_candidate(
            symbol, window, live_price=window[-1].c, instrument=inst)
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
        outcome, close_price = _resolve_forward(trade, bars[i + 1:])
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

    return {
        "market": source.name,
        "per_symbol": per_symbol,
        "all_trades": all_trades,
        "rejections": all_rej,
        "total_eval_days": total_eval_days,
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
    weeks = (stats["total_eval_days"] / 5.0) if stats["total_eval_days"] else 0

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
        lines.append(f"\n_Frequency: ~{n50 / weeks:.2f} resolved ≥50 / basket-week, "
                     f"~{n100 / weeks:.2f} dual-confluence / basket-week "
                     f"(~{weeks:.0f} basket-weeks of history)._\n")

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


def main() -> dict[str, Any]:
    stats = asyncio.run(run_replay())
    write_baseline(stats)
    return stats
