"""Pattern detectors beyond the SMC OB/BOS path — pure OHLC/swing arithmetic.

PATTERN_SCOPE.md §1: a detector is a pure function `detect_<name>(bars, ...) ->
PatternSetup | None`. It only has to emit (direction, entry zone, key levels); the
existing `levels.compute_levels_fx` + `store.walk_trade` do risk and resolution, so
nothing downstream changes shape. This module is the start of that protocol; the
first family is range breakout.

Every geometric parameter is PRE-REGISTERED and frozen in config
(`FX_RANGE_*`, see PATTERN_RANGE_BREAKOUT.md) — chosen a priori, not fit on TRAIN.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import smc_detector
from market_data import Bar
from v2 import config as cfg


@dataclass
class PatternSetup:
    """What a detector emits. `zone_low/high` feed compute_levels_fx unchanged."""
    pattern: str
    direction: str            # "long" | "short"
    zone_low: float
    zone_high: float
    measured_move: float      # structural target size (sanity bound; not the TP system)
    key_levels: dict[str, Any] = field(default_factory=dict)


def _cluster_level(prices: list[float], anchor: float, tol: float, *, above: bool) -> list[float]:
    """Prices within `tol` of `anchor` on the given side — the touches of a boundary.
    For resistance, touches sit at/below the ceiling (above=False); for support,
    at/above the floor (above=True)."""
    out = []
    for p in prices:
        d = p - anchor
        if abs(d) <= tol and ((d >= 0) if above else (d <= 0)):
            out.append(p)
    return out


def detect_range_breakout(bars: list[Bar], *, atr: float | None) -> PatternSetup | None:
    """Fire on the bar that first CLOSES beyond a well-formed horizontal range.

    Range (both boundaries required): within the last FX_RANGE_LOOKBACK bars,
    >= FX_RANGE_MIN_TOUCHES swing highs cluster within FX_RANGE_EQ_ATR*ATR of the
    ceiling R (= max swing high) and >= that many swing lows cluster within the
    same tol of the floor S (= min swing low). The cluster tolerance also enforces
    flatness, so no separate slope knob. Width R-S must be a real band
    (>= EQ_ATR*ATR) and contained (<= FX_RANGE_MAX_ATR*ATR — else it's a trend/
    triangle, not a range).

    Breakout: the CURRENT close is beyond a boundary by >= FX_RANGE_BRK_ATR*ATR AND
    the PRIOR close was inside — so it fires once, on the breakout bar, not every
    bar of a sustained move. A failed breakout is NOT a state machine: the trade's
    stop (0.5*ATR back toward the range, via compute_levels_fx) is the failure
    handler. Entry is the breakout CLOSE (global rule), passed as a thin zone.
    """
    if atr is None or atr <= 0:
        return None
    W = cfg.FX_RANGE_LOOKBACK
    win = bars[-W:] if len(bars) >= W else bars
    if len(win) < 20:
        return None

    sh_idx, sl_idx = smc_detector._find_swings(win)
    if len(sh_idx) < cfg.FX_RANGE_MIN_TOUCHES or len(sl_idx) < cfg.FX_RANGE_MIN_TOUCHES:
        return None

    sh_prices = [win[i].h for i in sh_idx]
    sl_prices = [win[i].l for i in sl_idx]
    R = max(sh_prices)                       # ceiling
    S = min(sl_prices)                       # floor
    tol = cfg.FX_RANGE_EQ_ATR * atr

    # Enough touches AT each boundary (flatness), and a real, contained band.
    if len(_cluster_level(sh_prices, R, tol, above=False)) < cfg.FX_RANGE_MIN_TOUCHES:
        return None
    if len(_cluster_level(sl_prices, S, tol, above=True)) < cfg.FX_RANGE_MIN_TOUCHES:
        return None
    width = R - S
    if width < tol or width > cfg.FX_RANGE_MAX_ATR * atr:
        return None

    buf = cfg.FX_RANGE_BRK_ATR * atr
    close, prev = bars[-1].c, bars[-2].c
    if close > R + buf and prev <= R + buf:
        direction, level = "long", R
    elif close < S - buf and prev >= S - buf:
        direction, level = "short", S
    else:
        return None

    return PatternSetup(
        pattern="range_breakout", direction=direction,
        zone_low=close, zone_high=close,      # entry at the confirmed breakout close
        measured_move=round(width, 5),
        key_levels={"resistance": round(R, 5), "support": round(S, 5),
                    "breakout_close": round(close, 5), "level": round(level, 5)},
    )
