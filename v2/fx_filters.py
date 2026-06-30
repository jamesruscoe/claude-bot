"""FX-native entry filters: trading session + correlation-aware exposure cap.

All pure functions / cheap lookups — no network. Gated by config and applied
only on the FX path at trade-open time, so they can only ever *block* a trade
(the conservative direction), never manufacture one.
"""
from __future__ import annotations

from datetime import datetime, timezone

from v2 import config as cfg


def _ccy(symbol: str) -> tuple[str, str]:
    """(base, quote) from a yfinance FX ticker e.g. 'EURUSD=X' -> ('EUR','USD')."""
    core = symbol.replace("=X", "")
    return core[:3].upper(), core[3:6].upper()


def currency_exposure(symbol: str, direction: str) -> dict[str, int]:
    """Signed per-currency exposure of a position. Long EURUSD => +EUR, -USD."""
    base, quote = _ccy(symbol)
    s = 1 if direction == "long" else -1
    return {base: s, quote: -s}


# ---- session ------------------------------------------------------------- #

def _in_window(hour: int, start: int, end: int) -> bool:
    return start <= hour < end if start < end else (hour >= start or hour < end)


def session_ok(symbol: str, now: datetime | None = None) -> tuple[bool, str]:
    """Is `now` an allowed session for `symbol` under the configured mode?"""
    mode = cfg.FX_SESSION_MODE
    if mode == "off":
        return True, "session filter off"
    now = now or datetime.now(timezone.utc)
    h = now.astimezone(timezone.utc).hour
    if mode == "overlap":
        ok = _in_window(h, *cfg.FX_OVERLAP_UTC)
        return ok, ("in London/NY overlap" if ok else "outside London/NY overlap")
    if mode == "skip_asia":
        _, quote = _ccy(symbol)
        base, _ = _ccy(symbol)
        if "JPY" in (base, quote):
            return True, "JPY pair — Asia session allowed"
        in_asia = _in_window(h, *cfg.FX_ASIA_UTC)
        return (not in_asia), ("thin Asia hours — skipped" if in_asia else "outside Asia hours")
    return True, "unknown session mode — allowing"


# ---- correlation cap ----------------------------------------------------- #

def correlation_cap_ok(symbol: str, direction: str,
                       open_trades: list[dict]) -> tuple[bool, str]:
    """Block a new trade if it would push any currency past FX_MAX_PER_CCY in the
    same direction across the open book. Treats e.g. long EURUSD + long GBPUSD as
    two USD-shorts toward the cap, so one macro view can't open as six tickets."""
    cap = cfg.FX_MAX_PER_CCY
    if cap <= 0:
        return True, "cap disabled"
    net: dict[str, int] = {}
    for t in open_trades:
        for c, s in currency_exposure(t["symbol"], t["direction"]).items():
            net[c] = net.get(c, 0) + s
    for c, s in currency_exposure(symbol, direction).items():
        projected = net.get(c, 0) + s
        if abs(projected) > cap:
            return False, (f"correlation cap — {c} exposure would hit {projected:+d} "
                           f"(cap +/-{cap})")
    return True, "within correlation cap"
