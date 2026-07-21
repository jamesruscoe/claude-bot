"""SMC detector — two setups only.

Setup 1: Order Block Retest
    Find the most recent strong impulsive move (3%+ over 1-3 candles).
    The "order block" is the last opposite-coloured candle BEFORE the impulse.
    Signal fires only when the current bar is the first bar to retrace into
    that OB zone after the impulse, and the OB has not been invalidated
    (no closing through it in the opposite direction).

Setup 2: Break of Structure Retest
    Find swings (high/low with 2 candles either side). Detect when a later bar
    closes beyond a swing extreme (BOS). Signal fires when the current bar is
    the first to pull back to retest that broken level, and the BOS has not
    been invalidated (no closing back through it).

Score:
    50 — exactly one setup fires
    100 — both setups fire and agree on direction
    0 — both fire but disagree (or none fire)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from market_data import Bar


# ---------- Tunables ----------

OB_IMPULSE_THRESHOLD = 0.03  # 3% over 1-3 candles
OB_LOOKBACK_BARS = 30        # how far back to search for the most recent impulse
OB_OPPOSITE_SEARCH = 5       # how far back to search for an opposite-colour OB
SWING_LOOKBACK = 2           # 2 candles either side
BOS_LOOKBACK_BARS = 30       # how far back to consider swings for a BOS retest


# ---------- Setup 1: Order Block Retest ----------

@dataclass
class OBRetest:
    direction: str            # "long" | "short"
    ob_index: int             # bar index of the order-block candle
    ob_high: float
    ob_low: float
    impulse_start: int
    impulse_end: int
    impulse_pct: float


def detect_ob_retest(
    bars: list[Bar],
    *,
    impulse_threshold: float = OB_IMPULSE_THRESHOLD,
    retest_window: int = 1,
    impulse_c2c: bool = False,
    impulse_max_len: int = 3,
    diag: dict | None = None,
) -> OBRetest | None:
    """`retest_window` (default 1) = how many recent bars the first retest of the
    OB zone may fall within (1 = must be the current bar; equities behaviour).
    `impulse_c2c` measures the impulse close-to-close instead of open-to-close
    (Yahoo FX daily opens are degenerate). `impulse_max_len` bounds the impulse
    window length in bars (default 3). NB: c2c anchors at close[start-1], one bar
    earlier than o2c's open[start], so an o2c run at max_len N spans the same
    price range as a c2c run at max_len N-1 — keep this in mind when comparing.
    `diag`, if given, is populated with the failure stage for rejection logging."""
    n = len(bars)
    if n < 6:
        return None

    # Walk backwards looking for the most recent qualifying impulse. The
    # impulse must end at least 1 bar before the current bar so there's room
    # for a retrace.
    earliest_end = max(2, n - 1 - OB_LOOKBACK_BARS)
    found = None
    for end_idx in range(n - 2, earliest_end - 1, -1):
        for length in range(1, impulse_max_len + 1):
            start_idx = end_idx - length + 1
            if start_idx < 1:
                continue
            cN = bars[end_idx].c
            base = bars[start_idx - 1].c if impulse_c2c else bars[start_idx].o
            if base <= 0:
                continue
            move = (cN - base) / base
            if move >= impulse_threshold:
                found = ("long", start_idx, end_idx, move)
                break
            if move <= -impulse_threshold:
                found = ("short", start_idx, end_idx, abs(move))
                break
        if found:
            break

    if not found:
        if diag is not None:
            diag["ob"] = "no_impulse"
        return None
    direction, start_idx, end_idx, impulse_pct = found

    # Origin candle: the last opposite-coloured candle just before the impulse.
    # Fall back to the first impulse candle if there isn't one nearby.
    ob_idx: int | None = None
    for j in range(start_idx - 1, max(start_idx - 1 - OB_OPPOSITE_SEARCH, -1), -1):
        if direction == "long" and bars[j].c < bars[j].o:
            ob_idx = j
            break
        if direction == "short" and bars[j].c > bars[j].o:
            ob_idx = j
            break
    if ob_idx is None:
        ob_idx = start_idx

    ob_high = bars[ob_idx].h
    ob_low = bars[ob_idx].l

    # Find the FIRST bar after the impulse to retrace into the OB zone. The
    # retest is valid only if that first touch falls within the last
    # `retest_window` bars (window=1 => it must be the current bar). A close
    # through the zone before any touch invalidates the OB.
    first_touch: int | None = None
    for k in range(end_idx + 1, n):
        if bars[k].l <= ob_high and bars[k].h >= ob_low:
            first_touch = k
            break
        if direction == "long" and bars[k].c < ob_low:
            if diag is not None:
                diag["ob"] = "ob_invalidated"
            return None
        if direction == "short" and bars[k].c > ob_high:
            if diag is not None:
                diag["ob"] = "ob_invalidated"
            return None

    if first_touch is None:
        if diag is not None:
            cur = bars[-1]
            if direction == "long" and ob_high:
                dist = (cur.c - ob_high) / ob_high
            elif ob_low:
                dist = (ob_low - cur.c) / ob_low
            else:
                dist = 0.0
            diag["ob"] = f"retest_missed:{dist * 100:.2f}%"
        return None
    if (n - 1) - first_touch > (retest_window - 1):
        if diag is not None:
            diag["ob"] = "retest_consumed"
        return None

    return OBRetest(
        direction=direction,
        ob_index=ob_idx,
        ob_high=round(ob_high, 5),
        ob_low=round(ob_low, 5),
        impulse_start=start_idx,
        impulse_end=end_idx,
        impulse_pct=round(impulse_pct, 4),
    )


# ---------- Setup 2: Break of Structure Retest ----------

@dataclass
class BOSRetest:
    direction: str            # "long" | "short"
    level: float              # the broken swing level (now acting as support/resistance)
    swing_index: int          # bar index of the original swing extreme
    broken_at: int            # bar index where price first closed beyond the level


def _find_swings(bars: list[Bar], lookback: int = SWING_LOOKBACK) -> tuple[list[int], list[int]]:
    sh: list[int] = []
    sl: list[int] = []
    for i in range(lookback, len(bars) - lookback):
        left = bars[i - lookback:i]
        right = bars[i + 1:i + 1 + lookback]
        if bars[i].h > max(b.h for b in left) and bars[i].h > max(b.h for b in right):
            sh.append(i)
        if bars[i].l < min(b.l for b in left) and bars[i].l < min(b.l for b in right):
            sl.append(i)
    return sh, sl


def detect_bos_retest(bars: list[Bar], *, retest_window: int = 1,
                      diag: dict | None = None) -> BOSRetest | None:
    """`retest_window` (default 1) = how many recent bars the first retest of the
    broken level may fall within. `diag`, if given, records the failure stage."""
    n = len(bars)
    if n < 10:
        return None

    sh, sl = _find_swings(bars)
    if not sh and not sl:
        if diag is not None:
            diag["bos"] = "no_swings"
        return None

    # For each swing, find the FIRST bar that closed beyond it. Among those
    # break events, take the most recent one inside our lookback window.
    candidates: list[tuple[str, float, int, int]] = []  # (direction, level, swing_idx, broken_at)

    for sh_idx in sh:
        level = bars[sh_idx].h
        for j in range(sh_idx + 1, n):
            if bars[j].c > level:
                candidates.append(("long", level, sh_idx, j))
                break

    for sl_idx in sl:
        level = bars[sl_idx].l
        for j in range(sl_idx + 1, n):
            if bars[j].c < level:
                candidates.append(("short", level, sl_idx, j))
                break

    if not candidates:
        if diag is not None:
            diag["bos"] = "no_break"
        return None

    # Most recent break, within the lookback window
    candidates.sort(key=lambda c: c[3], reverse=True)
    earliest = max(0, n - 1 - BOS_LOOKBACK_BARS)
    chosen: tuple[str, float, int, int] | None = None
    for c in candidates:
        if c[3] >= earliest and c[3] < n - 1:  # break must be before current
            chosen = c
            break
    if chosen is None:
        if diag is not None:
            diag["bos"] = "no_break"
        return None

    direction, level, swing_idx, broken_at = chosen

    # First retest of the broken level after the break. Valid only if that first
    # touch falls within the last `retest_window` bars; a close back through the
    # level before any touch invalidates the break.
    first_touch: int | None = None
    for k in range(broken_at + 1, n):
        touch = (bars[k].l <= level) if direction == "long" else (bars[k].h >= level)
        if touch:
            first_touch = k
            break
        if direction == "long" and bars[k].c < level:
            if diag is not None:
                diag["bos"] = "bos_invalidated"
            return None
        if direction == "short" and bars[k].c > level:
            if diag is not None:
                diag["bos"] = "bos_invalidated"
            return None

    if first_touch is None:
        if diag is not None:
            dist = abs(bars[-1].c - level) / level if level else 0.0
            diag["bos"] = f"bos_not_retested:{dist * 100:.2f}%"
        return None
    if (n - 1) - first_touch > (retest_window - 1):
        if diag is not None:
            diag["bos"] = "bos_consumed"
        return None

    return BOSRetest(direction=direction, level=round(level, 5),
                     swing_index=swing_idx, broken_at=broken_at)


# ---------- 50MA regime filter ----------

REGIME_MA_PERIOD = 50


def sma(bars: list[Bar], period: int = REGIME_MA_PERIOD) -> float | None:
    """Simple moving average of the last `period` closes. None if too few bars."""
    if len(bars) < period:
        return None
    return sum(b.c for b in bars[-period:]) / period


def regime_filter(bars: list[Bar], direction: str | None) -> str | None:
    """Block setups that go against the 50MA regime. Returns the block reason
    string, or None if the setup is clear to fire.

    Long setups blocked when current close is below the 50MA.
    Short setups blocked when current close is above the 50MA.
    Insufficient history → no block (don't penalise the early walk-forward
    window before the MA has converged)."""
    if direction is None:
        return None
    ma = sma(bars, REGIME_MA_PERIOD)
    if ma is None:
        return None
    current = bars[-1].c
    if direction == "long" and current < ma:
        return (
            f"against regime — 50MA filter blocked this setup "
            f"(price {current:.2f} < 50MA {ma:.2f})"
        )
    if direction == "short" and current > ma:
        return (
            f"against regime — 50MA filter blocked this setup "
            f"(price {current:.2f} > 50MA {ma:.2f})"
        )
    return None


# ---------- ATR (Wilder, 14-period default) — used for ATR-aware SL placement ----------

def atr(bars: list[Bar], period: int = 14) -> float | None:
    """Wilder's Average True Range. Returns the latest ATR, or None if there
    aren't enough bars."""
    if len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        h, l = bars[i].h, bars[i].l
        prev_c = bars[i - 1].c
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    avg = sum(trs[:period]) / period
    for tr in trs[period:]:
        avg = (avg * (period - 1) + tr) / period
    return avg


# ---------- Signal dedup helper (used by scan.py backtest + watch.py) ----------

def is_duplicate_signal(
    new: dict[str, Any],
    recent_fires: list[dict[str, Any]],
    *,
    current_idx: int,
    bars_window: int = 5,
    eps_pct: float = 0.005,
) -> bool:
    """True if a fire within the last `bars_window` bars matches `new` on
    direction, entry-zone bounds and stop-loss (within `eps_pct` relative
    tolerance). Caller maintains `recent_fires`; each entry must include
    `direction`, `entry_zone_low`, `entry_zone_high`, `stop_loss`, `bar_idx`.
    """
    for prev in recent_fires:
        if (current_idx - prev["bar_idx"]) > bars_window:
            continue
        if prev.get("direction") != new.get("direction"):
            continue
        match = True
        for key in ("entry_zone_low", "entry_zone_high", "stop_loss"):
            a, b = prev.get(key), new.get(key)
            if a is None or b is None:
                match = False
                break
            ref = max(abs(a), abs(b), 1.0)
            if abs(a - b) / ref > eps_pct:
                match = False
                break
        if match:
            return True
    return False


# ---------- Bias (display only — not used in scoring) ----------

def simple_bias(bars: list[Bar], window: int = 60) -> str:
    """Crude bias for dashboard display: % change over the last `window` bars."""
    if len(bars) < 5:
        return "ranging"
    recent = bars[-window:] if len(bars) >= window else bars
    first, last = recent[0].c, recent[-1].c
    if first <= 0:
        return "ranging"
    pct = (last - first) / first
    if pct > 0.02:
        return "bullish"
    if pct < -0.02:
        return "bearish"
    return "ranging"


# ---------- Scoring ----------

def score_setups(
    bars: list[Bar],
    *,
    impulse_threshold: float = OB_IMPULSE_THRESHOLD,
    retest_window: int = 1,
    impulse_c2c: bool = False,
    impulse_max_len: int = 3,
) -> tuple[int, str | None, dict[str, Any]]:
    """Return (score, direction, signals_dict).

    50  — exactly one setup fires
    100 — both setups fire AND agree on direction
    0   — none, both fire but disagree, or 50MA regime filter blocked it

    `impulse_threshold` overrides the default OB impulse percentage. `retest_window`
    and `impulse_c2c` are the FX detector calibrations (see detect_ob_retest). The
    per-stage failure reason for each detector is recorded in
    signals["ob_stage"]/["bos_stage"] for rejection logging.
    """
    diag: dict[str, str] = {}
    ob = detect_ob_retest(bars, impulse_threshold=impulse_threshold,
                          retest_window=retest_window, impulse_c2c=impulse_c2c,
                          impulse_max_len=impulse_max_len, diag=diag)
    bos = detect_bos_retest(bars, retest_window=retest_window, diag=diag)

    signals: dict[str, Any] = {"ob_retest": None, "bos_retest": None,
                               "ob_stage": diag.get("ob"), "bos_stage": diag.get("bos")}
    if ob:
        signals["ob_retest"] = {
            "direction": ob.direction,
            "ob_high": ob.ob_high,
            "ob_low": ob.ob_low,
            "impulse_pct": ob.impulse_pct,
            "impulse_start": ob.impulse_start,
            "impulse_end": ob.impulse_end,
            "ob_index": ob.ob_index,
        }
    if bos:
        signals["bos_retest"] = {
            "direction": bos.direction,
            "level": bos.level,
            "swing_index": bos.swing_index,
            "broken_at": bos.broken_at,
        }

    # Determine candidate score + direction first so we can filter by regime.
    if ob and bos:
        if ob.direction == bos.direction:
            score, direction = 100, ob.direction
        else:
            return 0, None, signals  # conflicting setups
    elif ob:
        score, direction = 50, ob.direction
    elif bos:
        score, direction = 50, bos.direction
    else:
        return 0, None, signals

    # 50MA regime filter — last gate before returning a fireable signal.
    block = regime_filter(bars, direction)
    if block:
        signals["regime_blocked"] = block
        return 0, None, signals

    return score, direction, signals


# ---------- Live-price staleness invalidation ----------

STALE_THRESHOLD_PCT = 0.02  # 2% — see invalidate_by_price docstring


def score_from_signals(signals: dict[str, Any]) -> tuple[int, str | None]:
    """Re-derive (score, direction) from a possibly-modified signals dict.
    Mirrors the scoring logic at the bottom of `score_setups`. Honours the
    `regime_blocked` flag — once the 50MA filter has rejected a setup,
    re-deriving from surviving signals must not resurrect it."""
    if signals.get("regime_blocked"):
        return 0, None
    ob = signals.get("ob_retest")
    bos = signals.get("bos_retest")
    if ob and bos:
        if ob["direction"] == bos["direction"]:
            return 100, ob["direction"]
        return 0, None
    if ob:
        return 50, ob["direction"]
    if bos:
        return 50, bos["direction"]
    return 0, None


def invalidate_by_price(
    signals: dict[str, Any],
    current_price: float | None,
    *,
    threshold_pct: float = STALE_THRESHOLD_PCT,
) -> tuple[dict[str, Any], list[str]]:
    """Drop OB/BOS signals where live price has moved >threshold_pct beyond
    the zone in the trade direction.

    The daily detector works on EOD bars, so a setup that fires after yesterday's
    close can be stale by the time we run today — if price has already run away
    from the OB zone or BOS level, "retest" is no longer in play. This is the
    intraday correction for that lag.

    Returns a new signals dict with the stale entries set to None, plus a list
    of human-readable reasons.
    """
    if current_price is None:
        return signals, []
    cleaned = dict(signals)
    reasons: list[str] = []

    ob = cleaned.get("ob_retest")
    if ob:
        if ob["direction"] == "long" and current_price > ob["ob_high"] * (1 + threshold_pct):
            reasons.append(
                f"OB zone left behind — price {current_price:.2f} moved away from "
                f"{ob['ob_low']}-{ob['ob_high']}"
            )
            cleaned["ob_retest"] = None
        elif ob["direction"] == "short" and current_price < ob["ob_low"] * (1 - threshold_pct):
            reasons.append(
                f"OB zone left behind — price {current_price:.2f} moved away from "
                f"{ob['ob_low']}-{ob['ob_high']}"
            )
            cleaned["ob_retest"] = None

    bos = cleaned.get("bos_retest")
    if bos:
        if bos["direction"] == "long" and current_price > bos["level"] * (1 + threshold_pct):
            reasons.append(
                f"BOS retest stale — price {current_price:.2f} moved away from level {bos['level']}"
            )
            cleaned["bos_retest"] = None
        elif bos["direction"] == "short" and current_price < bos["level"] * (1 - threshold_pct):
            reasons.append(
                f"BOS retest stale — price {current_price:.2f} moved away from level {bos['level']}"
            )
            cleaned["bos_retest"] = None

    return cleaned, reasons


# ---------- News sentiment confluence ----------

NEWS_VETO_CAP = 40  # hard ceiling when news disagrees with technical direction


def apply_news_sentiment(
    score: int,
    direction: str | None,
    news: dict[str, Any] | None,
) -> tuple[int, list[str]]:
    """Adjust the technical confluence score by news sentiment alignment.

    +15 when news sentiment matches the technical direction.
    Hard veto when they conflict — score is capped at NEWS_VETO_CAP (40)
    regardless of how strong the technical confluence is, and a warning is
    emitted. This is intentionally aggressive: trading against fresh
    headlines has historically been the largest source of avoidable losers.
    No change if news is neutral, missing, or there's no technical direction.

    Returns (adjusted_score, list_of_warnings).
    """
    if not news or not direction:
        return score, []
    sentiment = news.get("sentiment")
    if sentiment == "bullish" and direction == "long":
        return score + 15, []
    if sentiment == "bearish" and direction == "short":
        return score + 15, []
    if (sentiment == "bullish" and direction == "short") or \
       (sentiment == "bearish" and direction == "long"):
        capped = min(score, NEWS_VETO_CAP)
        return capped, [
            f"news conflicts with technical direction ({sentiment} news vs {direction} setup)"
        ]
    return score, []


# ---------- Compatibility helper for analyser/scan ----------

def analyse(bars: list[Bar]) -> dict[str, Any]:
    """Convenience wrapper used by scan/watch/analyser. Returns score, direction,
    signals, and the latest bar's close as `current_price`."""
    score, direction, signals = score_setups(bars)
    return {
        "score": score,
        "direction": direction,
        "signals": signals,
        "current_price": round(bars[-1].c, 5) if bars else None,
        "bias": simple_bias(bars),
    }
