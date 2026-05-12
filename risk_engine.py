"""Adaptive confidence and position-size logic driven by paper-trading
history. The base confluence-score buckets stay deterministic in
analyser._confidence_bucket; this module layers on what the paper log
actually says about each symbol's edge.

The thresholds below are deliberately conservative — they only fire once
a symbol has racked up enough trades for the win rate to be meaningful
(MIN_TRADES_FOR_ADJUSTMENT). Until then the brief stays at its base
confidence with an explicit "insufficient data" flag so consumers know
the number isn't load-bearing.
"""
from __future__ import annotations

import logging
from typing import Any

import paper_trader

log = logging.getLogger(__name__)

# Minimum closed trades before the win rate is treated as signal rather
# than noise. Below this we don't move the confidence label.
MIN_TRADES_FOR_ADJUSTMENT = 5

# Win-rate bands.
HIGH_WR = 0.65   # > this → "historically strong"
LOW_WR = 0.45    # < this → "below expected"
SKIP_WR = 0.35   # < this → "skip — insufficient edge"

# Loss-streak length at which we explicitly warn / down-size.
LOSS_STREAK_WARN = 3


def adjust_confidence(
    symbol: str,
    base_score: int | float,
    signals_detected: list[str] | None = None,
    news_sentiment: str | None = None,
) -> tuple[str, list[str]]:
    """Return (adjusted_confidence, warnings) for `symbol` given its paper
    track record. `base_score` and the unused `signals_detected` /
    `news_sentiment` parameters are accepted so callers can pass the whole
    signal context — the current implementation only consumes win rate and
    streak, but future heuristics (e.g. "downgrade on bearish news with
    long setup") can plug in without changing the call site.
    """
    base_confidence = _base_confidence(base_score)
    stats = paper_trader.get_symbol_stats(symbol)
    warnings: list[str] = []

    if stats["n"] < MIN_TRADES_FOR_ADJUSTMENT:
        warnings.append(
            f"insufficient data — only {stats['n']} closed paper trade(s) "
            f"on {symbol} (need {MIN_TRADES_FOR_ADJUSTMENT}+)"
        )
        if stats["current_loss_streak"] >= LOSS_STREAK_WARN:
            warnings.append(
                f"on a losing streak ({stats['current_loss_streak']} in a row) — "
                "reduce position size"
            )
        return base_confidence, warnings

    wr = stats["win_rate"]
    confidence = base_confidence
    if wr is not None:
        if wr > HIGH_WR:
            confidence = "high"
            warnings.append(
                f"historically strong setup — {wr * 100:.0f}% win rate over "
                f"{stats['n']} paper trades"
            )
        elif wr < LOW_WR:
            confidence = "low"
            warnings.append(
                f"below expected win rate on this symbol "
                f"({wr * 100:.0f}% over {stats['n']} paper trades)"
            )
        # 45–65% leaves confidence at its base value — middle of the road.

    if stats["current_loss_streak"] >= LOSS_STREAK_WARN:
        warnings.append(
            f"on a losing streak ({stats['current_loss_streak']} in a row) — "
            "reduce position size"
        )

    return confidence, warnings


def get_position_size_recommendation(
    symbol: str,
    account_size: float | None = None,
) -> dict[str, Any]:
    """Recommend a position-size bucket for `symbol`. `account_size` is
    accepted but currently unused — returning a bucket label (rather than
    a dollar figure) keeps the recommendation portable across capital
    levels. Future versions can multiply by account_size to derive shares.
    """
    stats = paper_trader.get_symbol_stats(symbol)
    n = stats["n"]
    wr = stats["win_rate"]
    win_streak = stats["current_win_streak"]
    loss_streak = stats["current_loss_streak"]

    # Decision priority (highest → lowest):
    #   1. wr below the skip floor    → skip
    #   2. active 3+ loss streak      → half
    #   3. wr above the strong band   → full (win streak doesn't downgrade it)
    #   4. <5 trades                  → quarter (no track record yet)
    #   5. middle of the road         → half
    if n >= MIN_TRADES_FOR_ADJUSTMENT and wr is not None and wr < SKIP_WR:
        return {
            "size": "skip — insufficient edge",
            "reason": f"{wr * 100:.0f}% win rate over {n} trades — below {int(SKIP_WR * 100)}% skip floor",
            "n": n,
        }

    if loss_streak >= LOSS_STREAK_WARN:
        return {
            "size": "half size",
            "reason": f"{loss_streak}-trade loss streak — caution until a win prints",
            "n": n,
        }

    if n >= MIN_TRADES_FOR_ADJUSTMENT and wr is not None and wr > HIGH_WR:
        streak_note = f" + {win_streak}-trade win streak" if win_streak >= 1 else ""
        return {
            "size": "full size",
            "reason": f"{wr * 100:.0f}% win rate over {n} trades{streak_note}",
            "n": n,
        }

    if n < MIN_TRADES_FOR_ADJUSTMENT:
        return {
            "size": "quarter size",
            "reason": f"only {n} closed paper trade(s) — building a track record",
            "n": n,
        }

    wr_str = f"{wr * 100:.0f}%" if wr is not None else "n/a"
    return {
        "size": "half size",
        "reason": f"{wr_str} win rate over {n} trades — middle of the road",
        "n": n,
    }


def _base_confidence(score: int | float) -> str:
    """Mirror analyser._confidence_bucket so this module can be invoked
    without circular-importing analyser."""
    if score >= 100:
        return "high"
    if score >= 75:
        return "medium"
    return "low"
