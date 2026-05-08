"""Atomic JSON-file storage for trade history. Thread-safe; tolerant of a
corrupted/missing file (returns empty list rather than crashing)."""
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from config import TRADES_FILE

log = logging.getLogger(__name__)

_lock = threading.Lock()


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
