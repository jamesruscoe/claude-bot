"""Signal engine — salvaged v1 detectors, v2 levels.

The pure-math SMC detection in smc_detector.py was the genuinely good part of
v1: the Order Block retest, Break-of-Structure retest, swings, ATR and the
50MA regime filter are all deterministic and testable, so we keep them as-is
and import them. What we DON'T reuse is v1's analyser (greedy liquidity
targets) and its scoring-as-decision — levels come from v2.levels and the
take/skip call comes from v2.brain.

This module's job: run the detectors on a symbol's daily bars and emit a single
`candidate` dict (or None + a rejection reason) for the rest of the pipeline to
reason about. The rejection reason is logged so "why did nothing fire" is
answerable from the ledger (audit fix), not just by external replay.

An optional `instrument` spec switches the risk math from price-based (equities)
to pip/spread-aware (FX) — see levels.compute_levels_fx.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import smc_detector  # salvaged v1 pure-math detectors
from market_data import Bar
from smc_detector import OB_IMPULSE_THRESHOLD
from v2 import levels
from v2.config import CANDIDATE_MIN_SCORE, OB_IMPULSE_OVERRIDES

# Thin band half-width around a BOS level when there's no OB zone to anchor to.
BOS_BAND_PCT = 0.0015


@dataclass
class Instrument:
    """Per-symbol risk-math context. None → equities (price-based)."""
    symbol: str
    pip_size: float
    spread_pips: float
    equity: float
    risk_pct: float
    std_lot: int


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


def _zero_score_reason(signals: dict[str, Any]) -> str:
    """Explain a score of 0 for rejection logging."""
    if signals.get("regime_blocked"):
        return "regime_blocked"
    if signals.get("ob_retest") and signals.get("bos_retest"):
        return "conflicting_setups"
    return "no_setup"


def build_candidate(
    symbol: str,
    bars: list[Bar],
    *,
    live_price: float | None = None,
    instrument: Instrument | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (candidate, None) on success, or (None, reason) on rejection.

    A returned candidate has passed: detector score >= floor, a directional
    zone, and the levels R:R floor. `instrument` (FX) switches the risk math to
    pip/spread-aware levels; absent it, the original price-based levels run.
    """
    if not bars or len(bars) < 20:
        return None, "too_few_bars"

    threshold = OB_IMPULSE_OVERRIDES.get(symbol, OB_IMPULSE_THRESHOLD)
    score, direction, signals = smc_detector.score_setups(bars, impulse_threshold=threshold)

    price = live_price if live_price is not None else bars[-1].c

    if score < CANDIDATE_MIN_SCORE or direction is None:
        return None, _zero_score_reason(signals)

    zone = _zone_from_signals(signals, direction)
    if zone is None:
        return None, "no_zone"
    zone_low, zone_high = zone

    atr = smc_detector.atr(bars)
    if instrument is not None:
        lv = levels.compute_levels_fx(
            direction, zone_low, zone_high, atr=atr, price=price,
            symbol=instrument.symbol, pip_size=instrument.pip_size,
            spread_pips=instrument.spread_pips, equity=instrument.equity,
            risk_pct=instrument.risk_pct, std_lot=instrument.std_lot,
        )
    else:
        lv = levels.compute_levels(direction, zone_low, zone_high, atr=atr, price=price)
    if lv is None:
        return None, "levels_rejected_wide_stop"

    candidate = {
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
    # FX bookkeeping fields (present only with an instrument spec).
    for k in ("risk_pips", "spread_pips", "lots"):
        if k in lv:
            candidate[k] = lv[k]
    return candidate, None
