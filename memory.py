"""Atomic JSON-file storage for trade history. Thread-safe; tolerant of a
corrupted/missing file (returns empty list rather than crashing)."""
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from config import FIRED_SIGNALS_FILE, TRADES_FILE

log = logging.getLogger(__name__)

_lock = threading.Lock()

# Stale-signal suppression: identical alerts firing within this window are
# treated as duplicates of an earlier fire. "Identical" means same symbol +
# direction with both entry-zone edges within SIGNAL_DEDUP_ZONE_PCT of the
# stored values — so a fresh signal in the same zone is suppressed, but if
# price has moved enough to shift the zone past the tolerance it fires.
SIGNAL_DEDUP_WINDOW_HOURS = 6
SIGNAL_DEDUP_ZONE_PCT = 0.005  # 0.5%


def _read_all() -> list[dict[str, Any]]:
    if not TRADES_FILE.exists():
        return []
    try:
        with TRADES_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read trades file: %s", e)
        return []


def _write_all(trades: list[dict[str, Any]]) -> None:
    tmp = TRADES_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2, default=str)
    tmp.replace(TRADES_FILE)


def add_trade(analysis: dict[str, Any]) -> dict[str, Any]:
    """Persist one trade analysis. Returns the stored record (with id+timestamp added)."""
    trade = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": analysis.get("symbol"),
        "direction": analysis.get("direction"),
        "entry": analysis.get("entry"),
        "entry_zone_low": analysis.get("entry_zone_low"),
        "entry_zone_high": analysis.get("entry_zone_high"),
        "stop_loss": analysis.get("stop_loss"),
        "take_profit_1": analysis.get("take_profit_1"),
        "take_profit_2": analysis.get("take_profit_2"),
        "best_window": analysis.get("best_window"),
        "rr_ratio": analysis.get("rr_ratio"),
        "confluence_score": analysis.get("confluence_score"),
        "confidence": analysis.get("confidence"),
        "htf_bias": analysis.get("htf_bias"),
        "patterns_detected": analysis.get("patterns_detected", []),
        "reasoning": analysis.get("reasoning"),
        "warnings": analysis.get("warnings", []),
        "take_trade": analysis.get("take_trade", False),
        "outcome": None,
    }
    with _lock:
        trades = _read_all()
        trades.append(trade)
        _write_all(trades)
    return trade


def get_last_trades_for_symbol(symbol: str, limit: int = 5) -> list[dict[str, Any]]:
    with _lock:
        trades = _read_all()
    matching = [t for t in trades if t.get("symbol") == symbol and t.get("take_trade")]
    return list(reversed(matching[-limit:]))


def get_recent_trades(limit: int = 10) -> list[dict[str, Any]]:
    with _lock:
        trades = _read_all()
    return list(reversed(trades[-limit:]))


def update_outcome(trade_id: str, outcome: str) -> dict[str, Any] | None:
    if outcome not in ("win", "loss", "stopped"):
        raise ValueError(f"Invalid outcome: {outcome}")
    with _lock:
        trades = _read_all()
        for trade in trades:
            if trade.get("id") == trade_id:
                trade["outcome"] = outcome
                trade["outcome_at"] = datetime.now(timezone.utc).isoformat()
                _write_all(trades)
                return trade
    return None


def total_count() -> int:
    with _lock:
        return len(_read_all())


def list_trades() -> list[dict[str, Any]]:
    """Snapshot of the entire trade log. Returns a copy."""
    with _lock:
        return list(_read_all())


# ---------- Fired-signal log (stale-signal suppression) ----------

def _read_fired_signals() -> list[dict[str, Any]]:
    if not FIRED_SIGNALS_FILE.exists():
        return []
    try:
        with FIRED_SIGNALS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read fired-signals file: %s", e)
        return []


def _write_fired_signals(items: list[dict[str, Any]]) -> None:
    tmp = FIRED_SIGNALS_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, default=str)
    tmp.replace(FIRED_SIGNALS_FILE)


def log_fired_signal(
    symbol: str,
    direction: str,
    entry_low: float,
    entry_high: float,
    sl: float,
    timestamp: str | datetime | None = None,
) -> None:
    """Persist the fact that a take-trade alert fired for this signal so
    subsequent runs can suppress identical re-fires via check_recent_signal.

    `timestamp` may be a datetime, ISO string, or None (defaults to now UTC).
    """
    if timestamp is None:
        ts = datetime.now(timezone.utc).isoformat()
    elif isinstance(timestamp, datetime):
        ts = timestamp.isoformat()
    else:
        ts = str(timestamp)
    record = {
        "symbol": symbol,
        "direction": direction,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": sl,
        "timestamp": ts,
    }
    with _lock:
        items = _read_fired_signals()
        items.append(record)
        # Keep the file bounded — far more than the 6-hour window needs, but
        # cheap and useful for after-the-fact debugging of suppression decisions.
        if len(items) > 500:
            items = items[-500:]
        _write_fired_signals(items)


def check_recent_signal(
    symbol: str,
    direction: str,
    entry_low: float,
    entry_high: float,
) -> bool:
    """Return True if an identical signal fired in the last
    SIGNAL_DEDUP_WINDOW_HOURS. "Identical" = same symbol+direction with both
    entry-zone edges within SIGNAL_DEDUP_ZONE_PCT of the stored values."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SIGNAL_DEDUP_WINDOW_HOURS)
    with _lock:
        items = _read_fired_signals()
    for item in items:
        if item.get("symbol") != symbol or item.get("direction") != direction:
            continue
        raw_ts = item.get("timestamp")
        if not raw_ts:
            continue
        try:
            ts = datetime.fromisoformat(str(raw_ts))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        old_low = item.get("entry_low")
        old_high = item.get("entry_high")
        if not old_low or not old_high:
            continue
        low_drift = abs(entry_low - old_low) / abs(old_low)
        high_drift = abs(entry_high - old_high) / abs(old_high)
        if low_drift <= SIGNAL_DEDUP_ZONE_PCT and high_drift <= SIGNAL_DEDUP_ZONE_PCT:
            return True
    return False


# ---------- Win rate ----------

def compute_win_rate(symbol: str) -> tuple[float, int] | None:
    """Rolling win rate for `symbol` from logged outcomes.

    Returns (win_rate, n_resolved) where n_resolved counts trades whose
    outcome is "win" or "loss" — "stopped" trades and unresolved (None)
    trades are excluded so the rate reflects market verdict only.

    Returns None if no resolved outcomes exist for the symbol.
    """
    with _lock:
        trades = _read_all()
    resolved = [
        t for t in trades
        if t.get("symbol") == symbol
        and t.get("outcome") in ("win", "loss")
    ]
    if not resolved:
        return None
    wins = sum(1 for t in resolved if t["outcome"] == "win")
    return (wins / len(resolved), len(resolved))
