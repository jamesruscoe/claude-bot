"""Entry / stop / target computation.

Two v1 bugs are fixed here:

1. Greedy targets. v1 derived TP1/TP2 from liquidity pools that routinely sat
   2.5-3.5 ATR away *regardless of risk*, so they almost never printed inside
   the 10-day window and trades bled to the stop or expired. v2 sets a TIGHT
   structure-based stop (0.5 ATR beyond the zone) and places targets as
   R-multiples of that risk — TP1 at 2R, TP2 at 3R. A tight stop keeps 1R
   small, so 2R is close enough to actually hit, and R:R is exact by
   construction. Setups whose stop is absurdly wide (> MAX_RISK_PCT of price)
   are rejected rather than handed a far target.

2. Best-case fills mislabelled as worst-case. v1 opened longs at the bottom of
   the zone and shorts at the top — the *most favourable* fill — which quietly
   inflated the paper track record. v2 fills at the zone midpoint, which is
   what a resting limit order in a retest actually gets.
"""
from __future__ import annotations

from typing import Any

from v2.config import MAX_RISK_PCT, SL_ATR_MULT, SL_BUFFER_PCT, TP1_R_MULT, TP2_R_MULT


def realistic_fill(zone_low: float, zone_high: float) -> float:
    """Midpoint of the retest zone — a neutral, honest fill assumption."""
    return round((zone_low + zone_high) / 2, 5)


def compute_levels(
    direction: str,
    zone_low: float,
    zone_high: float,
    *,
    atr: float | None,
    price: float | None = None,
) -> dict[str, Any] | None:
    """Return entry/stop/targets/RR for a candidate, or None if the stop is too
    wide to be a clean setup (risk > MAX_RISK_PCT of price).

    `direction` is "long" or "short"; the zone is the OB range or the thin BOS
    band. ATR sets the stop buffer; targets are R-multiples of the resulting
    risk, so they scale with the symbol's own volatility AND stay hit-able.
    """
    if direction not in ("long", "short"):
        return None
    if zone_high < zone_low:
        zone_low, zone_high = zone_high, zone_low

    fill = realistic_fill(zone_low, zone_high)

    if atr and atr > 0:
        sl_buf = SL_ATR_MULT * atr
        sl_source = "atr"
    else:
        sl_buf = fill * SL_BUFFER_PCT     # cold-start fallback before ATR exists
        sl_source = "pct_fallback"

    if direction == "long":
        stop = round(zone_low - sl_buf, 5)
        risk = fill - stop
    else:
        stop = round(zone_high + sl_buf, 5)
        risk = stop - fill

    if risk <= 0:
        return None
    # Reject a setup whose stop is implausibly far — a far stop means a far
    # target, the exact failure mode we're fixing.
    if price and price > 0 and (risk / price) > MAX_RISK_PCT:
        return None

    if direction == "long":
        tp1 = round(fill + TP1_R_MULT * risk, 5)
        tp2 = round(fill + TP2_R_MULT * risk, 5)
    else:
        tp1 = round(fill - TP1_R_MULT * risk, 5)
        tp2 = round(fill - TP2_R_MULT * risk, 5)

    return {
        "entry": fill,
        "entry_low": round(zone_low, 5),
        "entry_high": round(zone_high, 5),
        "stop_loss": stop,
        "tp1": tp1,
        "tp2": tp2,
        "rr": TP1_R_MULT,            # exact by construction
        "risk_pct": round(risk / price, 4) if price else None,
        "atr": round(atr, 5) if atr else None,
        "sl_source": sl_source,
    }
