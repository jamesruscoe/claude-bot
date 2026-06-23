"""Signal engine — salvaged v1 detectors, v2 levels.

The pure-math SMC detection in smc_detector.py was the genuinely good part of
v1: the Order Block retest, Break-of-Structure retest, swings, ATR and the
50MA regime filter are all deterministic and testable, so we keep them as-is
and import them. What we DON'T reuse is v1's analyser (greedy liquidity
targets) and its scoring-as-decision — levels come from v2.levels and the
take/skip call comes from v2.brain.

This module's job: run the detectors on a symbol's daily bars and emit a single
`candidate` dict (or None) for the rest of the pipeline to reason about.
"""
from __future__ import annotations

from typing import Any

import smc_detector  # salvaged v1 pure-math detectors
from market_data import Bar
from smc_detector import OB_IMPULSE_THRESHOLD
from v2 import levels
from v2.config import CANDIDATE_MIN_SCORE, OB_IMPULSE_OVERRIDES

# Thin band half-width around a BOS level when there's no OB zone to anchor to.
BOS_BAND_PCT = 0.0015


def _zone_from_signals(signals: dict[str, Any], direction: str) -> tuple[float, float] | None:
    """Pick the entry zone: prefer the OB range (a real range with history),
    fall back to a thin band around the BOS level."""
    ob = signals.get("ob_retest")
    if ob and ob["direction"] == direction:
        return float(ob["ob_low"]), float(ob["ob_high"])
    bos = signals.get("bos_retest")
    if bos and bos["direction"] == direction:
        lvl = float(bos["level"])
        return lvl * (1 - BOS_BAND_PCT), lvl * (1 + BOS_BAND_PCT)
    return None


def _setup_names(signals: dict[str, Any]) -> list[str]:
    names = []
    if signals.get("ob_retest"):
        names.append("ob_retest")
    if signals.get("bos_retest"):
        names.append("bos_retest")
    return names


def build_candidate(
    symbol: str,
    bars: list[Bar],
    *,
    live_price: float | None = None,
) -> dict[str, Any] | None:
    """Return a candidate dict, or None if nothing fireable on this symbol.

    A returned candidate has passed: detector score >= floor, a directional
    zone, the live-price staleness check, and the levels R:R floor.
    """
    if not bars or len(bars) < 20:
        return None

    threshold = OB_IMPULSE_OVERRIDES.get(symbol, OB_IMPULSE_THRESHOLD)
    score, direction, signals = smc_detector.score_setups(bars, impulse_threshold=threshold)

    price = live_price if live_price is not None else bars[-1].c

    # Salvaged staleness guard: if intraday price has already left the zone, the
    # retest is no longer in play — drop the stale signal and re-derive.
    signals, _stale = smc_detector.invalidate_by_price(signals, price)
    score, direction = smc_detector.score_from_signals(signals)

    if score < CANDIDATE_MIN_SCORE or direction is None:
        return None

    zone = _zone_from_signals(signals, direction)
    if zone is None:
        return None
    zone_low, zone_high = zone

    atr = smc_detector.atr(bars)
    lv = levels.compute_levels(direction, zone_low, zone_high, atr=atr, price=price)
    if lv is None:
        return None  # failed the R:R floor

    return {
        "symbol": symbol,
        "score": score,
        "direction": direction,
        "setups": _setup_names(signals),
        "regime": smc_detector.simple_bias(bars),
        "price": round(price, 5),
        "atr": lv["atr"],
        "entry": lv["entry"],
        "entry_low": lv["entry_low"],
        "entry_high": lv["entry_high"],
        "stop_loss": lv["stop_loss"],
        "tp1": lv["tp1"],
        "tp2": lv["tp2"],
        "rr": lv["rr"],
        "latest_bar_dt": bars[-1].dt,
    }
