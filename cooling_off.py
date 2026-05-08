"""Dynamic cooling-off blacklist.

Symbols whose rolling 30-day track record drops below COOLDOWN_WR_THRESHOLD
on at least COOLDOWN_MIN_RESOLVED resolved trades are automatically removed
from the live-alert pool for COOLDOWN_DAYS — the system continues to scan
them and surfaces their score, but `take_trade` is forced False so the
broken setup can't keep firing.

State persists across runs in COOLING_OFF_FILE. Each entry:
    {
      "started_at": "2026-05-08T21:00:00+00:00",
      "until":      "2026-06-07T21:00:00+00:00",
      "wins":       0,
      "losses":     3,
      "n":          3,
      "reason":     "0/3 in last 30 days",
      "source":     "dynamic" | "manual_seed",
    }

`bootstrap()` runs once on first import to seed any symbols listed in
config.INITIAL_COOLDOWN_SEED that aren't already tracked. `evaluate()`
re-derives dynamic entries from the live trade log every scan startup;
expired entries are pruned on every load.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from config import (
    COOLDOWN_DAYS,
    COOLDOWN_MIN_RESOLVED,
    COOLDOWN_WR_THRESHOLD,
    COOLING_OFF_FILE,
    INITIAL_COOLDOWN_SEED,
)

log = logging.getLogger(__name__)
_lock = threading.Lock()


# ---------- Persistence ----------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_raw() -> dict[str, dict[str, Any]]:
    if not COOLING_OFF_FILE.exists():
        return {}
    try:
        with COOLING_OFF_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read cooling_off file: %s", e)
        return {}


def _save(state: dict[str, dict[str, Any]]) -> None:
    tmp = COOLING_OFF_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    tmp.replace(COOLING_OFF_FILE)


def _is_active(entry: dict[str, Any], now: datetime | None = None) -> bool:
    if now is None:
        now = _now()
    until_str = entry.get("until")
    if not until_str:
        return False
    try:
        until = datetime.fromisoformat(str(until_str).replace("Z", "+00:00"))
    except ValueError:
        return False
    return until > now


def _load_pruned() -> dict[str, dict[str, Any]]:
    """Load and drop any expired entries (writing back if anything changed)."""
    raw = _load_raw()
    now = _now()
    pruned = {sym: entry for sym, entry in raw.items() if _is_active(entry, now)}
    if len(pruned) != len(raw):
        _save(pruned)
        dropped = sorted(set(raw) - set(pruned))
        log.info("Cooling-off expired and dropped: %s", dropped)
    return pruned


def _make_entry(
    wins: int,
    losses: int,
    *,
    reason: str,
    source: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if now is None:
        now = _now()
    until = now + timedelta(days=COOLDOWN_DAYS)
    return {
        "started_at": now.isoformat(),
        "until": until.isoformat(),
        "wins": int(wins),
        "losses": int(losses),
        "n": int(wins) + int(losses),
        "reason": reason,
        "source": source,
    }


# ---------- Public API ----------

def is_cooling_off(symbol: str) -> dict[str, Any] | None:
    """Active cooldown entry for `symbol` (post-prune), or None."""
    with _lock:
        state = _load_pruned()
    return state.get(symbol)


def current_state() -> dict[str, dict[str, Any]]:
    """Snapshot of all active cooldown entries (post-prune)."""
    with _lock:
        return _load_pruned()


def mark_cooling_off(
    symbol: str,
    *,
    wins: int,
    losses: int,
    reason: str,
    source: str = "dynamic",
) -> dict[str, Any]:
    """Force-add `symbol` to the cooldown list. Overwrites any existing entry."""
    entry = _make_entry(wins, losses, reason=reason, source=source)
    with _lock:
        state = _load_pruned()
        state[symbol] = entry
        _save(state)
    log.info("Cooling-off: %s — %s (%dW/%dL, until %s)",
             symbol, reason, wins, losses, entry["until"])
    return entry


def bootstrap() -> list[str]:
    """Seed manual cooldown entries on first run. Idempotent — symbols already
    present (active or otherwise) are not overwritten. Returns the list of
    newly-seeded symbols."""
    with _lock:
        raw = _load_raw()  # don't prune here; we want to know if seed already touched the file
        added: list[str] = []
        for sym, meta in INITIAL_COOLDOWN_SEED.items():
            if sym in raw:
                continue
            wins = int(meta.get("wins", 0))
            losses = int(meta.get("losses", 0))
            reason = str(meta.get("reason", f"{wins}/{wins + losses} resolved"))
            raw[sym] = _make_entry(wins, losses, reason=reason, source="manual_seed")
            added.append(sym)
        if added:
            _save(raw)
            log.info("Cooling-off bootstrapped: %s", added)
    return added


def evaluate(trades: list[dict[str, Any]]) -> list[str]:
    """Scan recent resolved outcomes; auto-add any symbol whose 30-day record
    has dropped below the threshold. Skips symbols already cooling off.

    `trades` is the full live trade log (memory.list_trades()).
    Returns the list of newly-added symbols.
    """
    now = _now()
    cutoff = now - timedelta(days=COOLDOWN_DAYS)
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        if t.get("outcome") not in ("win", "loss"):
            continue
        when_str = t.get("outcome_at") or t.get("timestamp")
        if not when_str:
            continue
        try:
            when = datetime.fromisoformat(str(when_str).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when < cutoff:
            continue
        sym = t.get("symbol")
        if not sym:
            continue
        by_symbol.setdefault(sym, []).append(t)

    added: list[str] = []
    with _lock:
        state = _load_pruned()
        for sym, recent in by_symbol.items():
            if sym in state:
                continue  # already cooling off
            n = len(recent)
            if n < COOLDOWN_MIN_RESOLVED:
                continue
            wins = sum(1 for t in recent if t["outcome"] == "win")
            losses = n - wins
            if (wins / n) >= COOLDOWN_WR_THRESHOLD:
                continue
            reason = f"{wins}/{n} in last {COOLDOWN_DAYS} days"
            state[sym] = _make_entry(wins, losses, reason=reason, source="dynamic")
            added.append(sym)
        if added:
            _save(state)
            log.info("Cooling-off auto-added: %s", added)
    return added
