"""Autonomous paper-trading ledger.

Every take_trade signal that fires from scan.py is opened here as a virtual
trade. On each subsequent scan, `check_open_trades` resolves any that have
hit TP1, TP2, SL, or aged past PAPER_TRADE_EXPIRY_DAYS trading days. The
resulting track record is what `risk_engine` uses to adjust confidence and
position sizing — i.e. the bot self-calibrates from real outcomes rather
than from baked-in priors.

Storage is JSON on disk (PAPER_TRADES_FILE), guarded by a process-local
lock. No external DB — the file is small enough that full rewrites on
every mutation are fine and the atomic-rename pattern keeps it durable.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

from config import PAPER_TRADES_FILE

log = logging.getLogger(__name__)

_lock = threading.Lock()

# Trades older than this many trading (weekday) days that have neither
# tagged TP nor SL are force-closed at the current market price and
# tagged EXPIRED. Matches the 10-day backtest horizon so the paper log
# is comparable to historical outcomes.
PAPER_TRADE_EXPIRY_DAYS = 10

# Outcome string constants — exported so consumers can switch on them
# without re-spelling the literals.
OUTCOME_WIN_TP1 = "WIN_TP1"
OUTCOME_WIN_TP2 = "WIN_TP2"
OUTCOME_LOSS = "LOSS"
OUTCOME_EXPIRED = "EXPIRED"

_WIN_OUTCOMES = {OUTCOME_WIN_TP1, OUTCOME_WIN_TP2}
_LOSS_OUTCOMES = {OUTCOME_LOSS}


# ---------- File IO ----------

def _read_all() -> list[dict[str, Any]]:
    if not PAPER_TRADES_FILE.exists():
        return []
    try:
        with PAPER_TRADES_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read paper trades file: %s", e)
        return []


def _write_all(trades: list[dict[str, Any]]) -> None:
    tmp = PAPER_TRADES_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2, default=str)
    tmp.replace(PAPER_TRADES_FILE)


# ---------- Lifecycle ----------

def open_paper_trade(
    symbol: str,
    direction: str,
    entry_price: float,
    sl: float,
    tp1: float,
    tp2: float,
    signal_score: float | int,
    signals_detected: list[str] | None = None,
    news_sentiment: str | None = None,
    regime: str | None = None,
    timestamp: str | datetime | None = None,
) -> dict[str, Any]:
    """Create a new open paper trade. Returns the stored record."""
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    if timestamp is None:
        opened_at = datetime.now(timezone.utc).isoformat()
    elif isinstance(timestamp, datetime):
        opened_at = timestamp.isoformat()
    else:
        opened_at = str(timestamp)

    record = {
        "id": str(uuid.uuid4()),
        "symbol": symbol,
        "direction": direction,
        "entry_price": float(entry_price),
        "sl": float(sl),
        "tp1": float(tp1),
        "tp2": float(tp2),
        "score": signal_score,
        "signals": list(signals_detected or []),
        "news_sentiment": news_sentiment,
        "regime": regime,
        "opened_at": opened_at,
        "closed_at": None,
        "close_price": None,
        "outcome": None,
        "pnl_r": None,
    }
    with _lock:
        trades = _read_all()
        trades.append(record)
        _write_all(trades)
    log.info("paper trade opened: %s %s @ %s (sl %s, tp1 %s, tp2 %s)",
             symbol, direction, entry_price, sl, tp1, tp2)
    return record


def _trading_days_between(start: datetime, end: datetime) -> int:
    """Inclusive count of weekdays (Mon-Fri) between two datetimes,
    using calendar dates only (timezone differences within a day collapse
    to zero). Mirrors how the backtest counts horizon bars."""
    if end < start:
        return 0
    d0: date = start.date()
    d1: date = end.date()
    days = 0
    cur = d0
    while cur < d1:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def _pnl_r(entry: float, sl: float, close_price: float, direction: str) -> float:
    """R-multiple of the realised P&L. 1R = the initial risk distance
    (entry − sl for longs, sl − entry for shorts). +2R at TP1, +3R at
    TP2, ≈-1R at SL."""
    risk = abs(entry - sl)
    if risk <= 0:
        return 0.0
    if direction == "long":
        return (close_price - entry) / risk
    return (entry - close_price) / risk


def _resolve_trade(
    trade: dict[str, Any],
    current_price: float,
    now: datetime,
) -> str | None:
    """Decide whether `trade` should close at `current_price` right now.
    Returns the outcome string, or None if the trade stays open."""
    direction = trade.get("direction")
    if direction not in ("long", "short"):
        return None
    sl = trade["sl"]
    tp1 = trade["tp1"]
    tp2 = trade["tp2"]

    # We only see a single price snapshot per scan, so we can't tell
    # intra-bar ordering — apply a conservative priority: SL first
    # (worst-case slippage on the loss side), then TP2 (best case so we
    # don't downgrade a runner to TP1), then TP1.
    if direction == "long":
        if current_price <= sl:
            return OUTCOME_LOSS
        if current_price >= tp2:
            return OUTCOME_WIN_TP2
        if current_price >= tp1:
            return OUTCOME_WIN_TP1
    else:
        if current_price >= sl:
            return OUTCOME_LOSS
        if current_price <= tp2:
            return OUTCOME_WIN_TP2
        if current_price <= tp1:
            return OUTCOME_WIN_TP1

    # Time-based expiry. Compare against the trade's open timestamp.
    try:
        opened = datetime.fromisoformat(str(trade["opened_at"]))
    except (KeyError, ValueError):
        return None
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    if _trading_days_between(opened, now) >= PAPER_TRADE_EXPIRY_DAYS:
        return OUTCOME_EXPIRED
    return None


def check_open_trades(current_prices: dict[str, float]) -> list[dict[str, Any]]:
    """Close any open trade whose live price has crossed SL/TP1/TP2 or that
    has aged past the expiry window. Returns the list of newly closed
    trades (already persisted)."""
    now = datetime.now(timezone.utc)
    closed: list[dict[str, Any]] = []
    with _lock:
        trades = _read_all()
        dirty = False
        for trade in trades:
            if trade.get("outcome") is not None:
                continue
            symbol = trade.get("symbol")
            price = current_prices.get(symbol)
            if price is None:
                # No live price for this symbol on the current scan —
                # might be a paper trade for a symbol we've since dropped
                # from the watchlist. Leave it open for now; a later scan
                # that re-includes the symbol will resolve it.
                continue
            outcome = _resolve_trade(trade, price, now)
            if outcome is None:
                continue
            if outcome == OUTCOME_EXPIRED:
                close_price = float(price)
            else:
                # For SL/TP outcomes, use the threshold the price crossed
                # rather than the noisy intra-scan tick — that way the
                # R-multiple lines up exactly with the +2R / +3R / -1R
                # expectation instead of drifting on whichever scan tick
                # happened to register the cross.
                if outcome == OUTCOME_WIN_TP1:
                    close_price = float(trade["tp1"])
                elif outcome == OUTCOME_WIN_TP2:
                    close_price = float(trade["tp2"])
                else:
                    close_price = float(trade["sl"])
            trade["outcome"] = outcome
            trade["close_price"] = close_price
            trade["closed_at"] = now.isoformat()
            trade["pnl_r"] = round(
                _pnl_r(trade["entry_price"], trade["sl"], close_price, trade["direction"]),
                3,
            )
            closed.append(dict(trade))
            dirty = True
            log.info("paper trade closed: %s %s → %s @ %s (%.2fR)",
                     symbol, trade["direction"], outcome, close_price, trade["pnl_r"])
        if dirty:
            _write_all(trades)
    return closed


# ---------- Read accessors ----------

def list_all() -> list[dict[str, Any]]:
    """Snapshot of every paper trade (open + closed). Returns a copy."""
    with _lock:
        return list(_read_all())


def list_open() -> list[dict[str, Any]]:
    return [t for t in list_all() if t.get("outcome") is None]


def list_closed() -> list[dict[str, Any]]:
    return [t for t in list_all() if t.get("outcome") is not None]


# ---------- Stats ----------

def _is_win(outcome: str | None) -> bool:
    return outcome in _WIN_OUTCOMES


def _is_loss(outcome: str | None) -> bool:
    return outcome in _LOSS_OUTCOMES


def _closed_for(symbol: str) -> list[dict[str, Any]]:
    """Closed trades for `symbol`, sorted oldest → newest by closed_at."""
    closed = [t for t in list_all()
              if t.get("symbol") == symbol and t.get("outcome") is not None]
    closed.sort(key=lambda t: t.get("closed_at") or "")
    return closed


def _streak_at_tail(trades: list[dict[str, Any]]) -> tuple[int, int]:
    """Walk back from the most recent trade. Returns (win_streak, loss_streak).
    Only one of the two will be non-zero (the most recent result kind).
    EXPIRED breaks both streaks unless it's a positive-R expiry, which
    counts as a win for streak purposes."""
    win = loss = 0
    for t in reversed(trades):
        outcome = t.get("outcome")
        if _is_win(outcome):
            if loss:
                break
            win += 1
        elif _is_loss(outcome):
            if win:
                break
            loss += 1
        else:
            # EXPIRED. Tag by R sign so a small green expiry still counts
            # as a streak extender, but only when the streak it would
            # extend is the matching kind.
            r = t.get("pnl_r") or 0
            if r > 0 and not loss:
                win += 1
            elif r < 0 and not win:
                loss += 1
            else:
                break
    return win, loss


def get_symbol_stats(symbol: str) -> dict[str, Any]:
    """Per-symbol track record. `meaningful` flips to True once the symbol
    has at least 5 closed trades — below that the numbers are noise."""
    closed = _closed_for(symbol)
    n = len(closed)
    if n == 0:
        return {
            "symbol": symbol,
            "n": 0,
            "meaningful": False,
            "win_rate": None,
            "avg_r": None,
            "best_pattern": None,
            "worst_condition": None,
            "current_win_streak": 0,
            "current_loss_streak": 0,
        }

    wins = sum(1 for t in closed if _is_win(t.get("outcome")))
    losses = sum(1 for t in closed if _is_loss(t.get("outcome")))
    decided = wins + losses
    win_rate = (wins / decided) if decided else None
    rs = [t.get("pnl_r") for t in closed if t.get("pnl_r") is not None]
    avg_r = (sum(rs) / len(rs)) if rs else None

    # best_pattern: signal name appearing most often in winning trades.
    win_signals: Counter[str] = Counter()
    for t in closed:
        if _is_win(t.get("outcome")):
            for s in t.get("signals") or []:
                win_signals[s] += 1
    best_pattern = win_signals.most_common(1)[0][0] if win_signals else None

    # worst_condition: news_sentiment / regime label with the lowest win
    # rate (min sample size 2 to avoid one-off labels stealing the title).
    cond_buckets: dict[str, list[bool]] = {}
    for t in closed:
        outcome = t.get("outcome")
        is_decided = _is_win(outcome) or _is_loss(outcome)
        if not is_decided:
            continue
        for key in (t.get("news_sentiment"), t.get("regime")):
            if not key:
                continue
            cond_buckets.setdefault(str(key), []).append(_is_win(outcome))
    worst_condition = None
    worst_rate = 1.1
    for label, bucket in cond_buckets.items():
        if len(bucket) < 2:
            continue
        rate = sum(bucket) / len(bucket)
        if rate < worst_rate:
            worst_rate = rate
            worst_condition = label

    win_streak, loss_streak = _streak_at_tail(closed)

    return {
        "symbol": symbol,
        "n": n,
        "meaningful": n >= 5,
        "win_rate": win_rate,
        "avg_r": avg_r,
        "best_pattern": best_pattern,
        "worst_condition": worst_condition,
        "current_win_streak": win_streak,
        "current_loss_streak": loss_streak,
    }


def get_system_stats() -> dict[str, Any]:
    """Overall paper-trading scorecard across every symbol."""
    closed = [t for t in list_all() if t.get("outcome") is not None]
    open_n = sum(1 for t in list_all() if t.get("outcome") is None)
    n = len(closed)
    if n == 0:
        return {
            "total_trades": 0,
            "open_trades": open_n,
            "win_rate": None,
            "profit_factor": None,
            "total_r": 0.0,
            "best_symbol": None,
            "worst_symbol": None,
            "current_win_streak": 0,
            "current_loss_streak": 0,
        }

    wins = sum(1 for t in closed if _is_win(t.get("outcome")))
    losses = sum(1 for t in closed if _is_loss(t.get("outcome")))
    decided = wins + losses
    win_rate = (wins / decided) if decided else None

    win_r = sum((t.get("pnl_r") or 0) for t in closed if _is_win(t.get("outcome")))
    loss_r = sum((t.get("pnl_r") or 0) for t in closed if _is_loss(t.get("outcome")))
    profit_factor = (win_r / abs(loss_r)) if loss_r else (float("inf") if win_r else None)
    total_r = sum((t.get("pnl_r") or 0) for t in closed)

    by_symbol: dict[str, dict[str, float]] = {}
    for t in closed:
        sym = t.get("symbol") or "?"
        bucket = by_symbol.setdefault(sym, {"wins": 0, "decided": 0, "r": 0.0})
        if _is_win(t.get("outcome")):
            bucket["wins"] += 1
            bucket["decided"] += 1
        elif _is_loss(t.get("outcome")):
            bucket["decided"] += 1
        bucket["r"] += t.get("pnl_r") or 0
    # Need 3+ trades to be a meaningful best/worst candidate.
    candidates = [
        (sym, b["wins"] / b["decided"] if b["decided"] else 0.0, b["r"])
        for sym, b in by_symbol.items() if b["decided"] >= 3
    ]
    if candidates:
        best_symbol = max(candidates, key=lambda x: (x[1], x[2]))[0]
        worst_symbol = min(candidates, key=lambda x: (x[1], x[2]))[0]
    else:
        best_symbol = worst_symbol = None

    chronological = sorted(closed, key=lambda t: t.get("closed_at") or "")
    win_streak, loss_streak = _streak_at_tail(chronological)

    return {
        "total_trades": n,
        "open_trades": open_n,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "total_r": round(total_r, 3),
        "best_symbol": best_symbol,
        "worst_symbol": worst_symbol,
        "current_win_streak": win_streak,
        "current_loss_streak": loss_streak,
    }


def compute_unrealised(open_trades: list[dict[str, Any]],
                       prices: dict[str, float]) -> list[dict[str, Any]]:
    """Annotate open trades with `current_price` and `unrealised_r` based on
    the latest live prices. Used by the dashboard to render running P&L."""
    out: list[dict[str, Any]] = []
    for t in open_trades:
        clone = dict(t)
        price = prices.get(t.get("symbol"))
        if price is None:
            clone["current_price"] = None
            clone["unrealised_r"] = None
        else:
            clone["current_price"] = float(price)
            clone["unrealised_r"] = round(
                _pnl_r(t["entry_price"], t["sl"], float(price), t["direction"]),
                3,
            )
        out.append(clone)
    return out
