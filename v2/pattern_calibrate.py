"""Per-pattern TRAIN-only calibration (Gate 1 in-sample screen).

Walks TRAIN data only (OANDA daily, pre-2021-01-01), runs a single pattern detector
in ISOLATION, opens paper trades via the existing pip/spread levels, resolves them
with the honest intrabar `walk_trade`, and reports frequency + expectancy NET of the
MEASURED bid/ask spread with a bootstrap CI. ONE measurement at the PRE-REGISTERED
parameters — no sweep. The holdout is never touched.

Gate 1 (per PATTERN_RANGE_BREAKOUT.md): keep the pattern iff TRAIN mean R is
non-negative net of measured spread AND frequency is material (>= ~8/yr). This is an
in-sample screen, NOT validation — passing only earns a forward trial at probation.
"""
from __future__ import annotations

import asyncio
import logging
from statistics import mean
from typing import Any, Callable

import smc_detector
from config import BACKTEST_WARMUP_BARS
from market_data import Bar
from v2 import config as cfg
from v2 import levels, patterns, replay, store
from v2.oanda_baseline import bootstrap_mean_ci, split_train_holdout
from v2.oanda_source import OANDASource

log = logging.getLogger(__name__)

_MATERIAL_FREQ_PER_YR = 8.0  # pre-registered "material frequency" bar

# Detector registry for calibration. Each returns a PatternSetup | None.
_DETECTORS: dict[str, Callable[..., Any]] = {
    "range_breakout": patterns.detect_range_breakout,
}


def _walk_pattern(symbol: str, bars: list[Bar], detector: Callable[..., Any],
                  *, spread_pips: float) -> list[dict[str, Any]]:
    """Walk `bars` (TRAIN), open one paper trade per fresh setup (deduped per
    direction over the expiry window), resolve forward with walk_trade. Returns
    resolved trades with raw (unsized) R net of the measured spread."""
    trades: list[dict[str, Any]] = []
    open_dirs: dict[str, int] = {}
    pip = cfg.fx_pip_size(symbol)
    n = len(bars)
    start = max(BACKTEST_WARMUP_BARS, 20)
    fwd_cap = cfg.EXPIRY_TRADING_DAYS * 3 + 5
    for i in range(start, n):
        lo = max(0, i + 1 - cfg.FX_LIVE_DAILY_LOOKBACK)  # live ~3yr view (fidelity)
        window = bars[lo:i + 1]
        atr = smc_detector.atr(window)
        setup = detector(window, atr=atr)
        if setup is None:
            continue
        d = setup.direction
        if open_dirs.get(d, -1) > i:      # de-dup: one open per direction at a time
            continue
        lv = levels.compute_levels_fx(
            d, setup.zone_low, setup.zone_high, atr=atr, price=window[-1].c,
            symbol=symbol, pip_size=pip, spread_pips=spread_pips,
            equity=cfg.FX_ACCOUNT_EQUITY, risk_pct=cfg.FX_RISK_PCT,
            std_lot=cfg.FX_STD_LOT_UNITS)
        if lv is None:
            continue
        trade = {
            "symbol": symbol, "direction": d,
            "entry_price": lv["entry"], "stop_loss": lv["stop_loss"],
            "original_sl": lv["stop_loss"], "tp1": lv["tp1"], "tp2": lv["tp2"],
            "tp1_hit": 0, "tp1_hit_at": None,
            "opened_at": bars[i].dt.isoformat(), "_opened_dt": bars[i].dt,
        }
        outcome, close_price = replay._resolve_forward(trade, bars[i + 1:i + 1 + fwd_cap])
        if outcome is not None:
            raw_r = round(store._pnl_r(lv["entry"], lv["stop_loss"], close_price, d), 3)
            trades.append({"outcome": outcome, "raw_r": raw_r})
            open_dirs[d] = min(i + cfg.EXPIRY_TRADING_DAYS, n)
        else:
            open_dirs[d] = n
    return trades


async def run_calibration(pattern: str) -> dict[str, Any]:
    if pattern not in _DETECTORS:
        raise ValueError(f"unknown pattern {pattern!r}; known: {list(_DETECTORS)}")
    if not cfg.FX_OANDA:
        raise RuntimeError("run with BOT_MARKET=fx_oanda (OANDA data + FX math)")
    detector = _DETECTORS[pattern]
    source = OANDASource()
    all_r: list[float] = []
    wins = losses = 0
    train_years = 0.0
    per_symbol: list[dict[str, Any]] = []
    for symbol in source.symbols():
        bars = await source.fetch_daily(symbol)
        if len(bars) < 60:
            continue
        train, _holdout = split_train_holdout(bars)      # holdout discarded, untouched
        sp = source.measured_spread_stats(symbol, train)
        spread = sp["median"] if sp else cfg.fx_spread_pips(symbol)
        tr = _walk_pattern(symbol, train, detector, spread_pips=spread)
        rs = [t["raw_r"] for t in tr]
        w = sum(1 for t in tr if t["outcome"] in replay._WIN)
        loss = sum(1 for t in tr if t["outcome"] in replay._LOSS)
        wins += w
        losses += loss
        all_r += rs
        if train:
            train_years = max(train_years, (train[-1].dt - train[0].dt).days / 365.25)
        per_symbol.append({"symbol": symbol, "n": len(rs),
                           "mean_r": (mean(rs) if rs else None), "spread": spread})
    await source.aclose()

    n = len(all_r)
    decided = wins + losses
    ci = bootstrap_mean_ci(all_r) if n >= 2 else None
    freq = (n / train_years) if train_years else 0.0
    return {
        "pattern": pattern, "n": n, "wins": wins, "losses": losses,
        "win_rate": (wins / decided) if decided else None,
        "mean_r": (mean(all_r) if all_r else None),
        "total_r": round(sum(all_r), 2) if all_r else 0.0,
        "ci": ci, "train_years": train_years, "freq_per_yr": freq,
        "per_symbol": per_symbol,
    }


def _fmt_r(x): return f"{x:+.3f}R" if x is not None else "n/a"
def _fmt_pct(x): return f"{x * 100:.0f}%" if x is not None else "n/a"


def verdict(res: dict[str, Any]) -> tuple[bool, str]:
    mr, freq = res["mean_r"], res["freq_per_yr"]
    if mr is None:
        return False, ("**SHELVE — no resolved trades on TRAIN.** The detector, at its "
                       "pre-registered parameters, essentially never fires. Nothing to carry forward.")
    nonneg = mr >= 0
    material = freq >= _MATERIAL_FREQ_PER_YR
    if nonneg and material:
        return True, (f"**KEEP (passes Gate 1 in-sample screen).** TRAIN mean {_fmt_r(mr)} "
                      f"net of measured spread (>=0) at ~{freq:.1f}/yr (>= {_MATERIAL_FREQ_PER_YR:.0f}/yr). "
                      "This earns range breakout a FORWARD trial at probation size only — it is "
                      "NOT validation (in-sample, geometry chosen on this era). Confidence stays "
                      "`unproven` until forward n accrues (P0).")
    reasons = []
    if not nonneg:
        reasons.append(f"TRAIN mean {_fmt_r(mr)} is NEGATIVE net of measured spread")
    if not material:
        reasons.append(f"frequency ~{freq:.1f}/yr is below the {_MATERIAL_FREQ_PER_YR:.0f}/yr bar "
                       "(could never accrue n>=150 forward in a sane horizon)")
    return False, ("**SHELVE — fails Gate 1.** " + "; ".join(reasons) +
                   ". Per the pre-registration we do NOT re-open the parameters to rescue it.")


def _write_result(res: dict[str, Any], keep: bool, verdict_text: str,
                  path: str = "PATTERN_RANGE_BREAKOUT.md") -> None:
    c = res["ci"]
    lines = [
        f"_Measured once at the pre-registered parameters — {res['train_years']:.1f} yr of "
        "TRAIN (OANDA daily, pre-2021-01-01), net of measured bid/ask, honest intrabar "
        "resolution. Holdout untouched._\n",
        "| Metric | Value |", "|--------|------:|",
        f"| Resolved trades (n) | {res['n']} |",
        f"| Frequency | ~{res['freq_per_yr']:.1f}/yr (basket) |",
        f"| Win rate | {_fmt_pct(res['win_rate'])} |",
        f"| Mean R (net measured spread) | {_fmt_r(res['mean_r'])} |",
        f"| Total R | {res['total_r']:+.2f} |",
    ]
    if c:
        lines.append(f"| One-sided 95% bootstrap lower bound | {_fmt_r(c['one_sided_95_lower'])} |")
        lines.append(f"| Two-sided 95% CI | [{c['two_sided_95_lower']:+.3f}, "
                     f"{c['two_sided_95_upper']:+.3f}] R |")
    lines.append(f"\n### Gate 1 verdict\n\n{verdict_text}\n")
    block = "\n".join(lines)

    try:
        text = open(path, encoding="utf-8").read()
        placeholder = "_Appended by `python run.py --pattern-calibrate range_breakout`._"
        text = text.replace(placeholder, block) if placeholder in text else text + "\n" + block
        open(path, "w", encoding="utf-8").write(text)
        log.info("wrote Gate 1 result into %s", path)
    except OSError as e:
        log.warning("could not write result doc: %s", e)


def main(pattern: str) -> dict[str, Any]:
    res = asyncio.run(run_calibration(pattern))
    keep, vtext = verdict(res)
    _write_result(res, keep, vtext)
    print(f"\n=== Gate 1 — {pattern} (TRAIN, net measured spread) ===")
    print(f"n={res['n']}  ~{res['freq_per_yr']:.1f}/yr  WR={_fmt_pct(res['win_rate'])}  "
          f"mean={_fmt_r(res['mean_r'])}  "
          f"LB={_fmt_r(res['ci']['one_sided_95_lower']) if res['ci'] else 'n/a'}")
    print(vtext)
    return res
