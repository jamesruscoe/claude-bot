"""Scheduled high-impact news avoidance (ForexFactory weekly JSON).

Blocks opening a trade within +/- a window of a high-impact event for either of
the pair's currencies. Fetches the free FF weekly feed, caches it under
STATE_DIR, and **fails open**: if the feed can't be fetched or parsed we log a
warning and allow the trade (a feed outage must not silently freeze the bot),
but the live caller records that the check couldn't run.

The feed is intentionally the only networked filter; everything else in Phase 2
is pure. Parsing is isolated from fetching so it can be unit-tested offline.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from v2 import config as cfg
from v2.fx_filters import _ccy

log = logging.getLogger(__name__)

_CACHE_TTL = 3600  # 1h — the weekly feed barely changes intraday


def _cache_path():
    return cfg.STATE_DIR / "ff_calendar.json"


def _parse_events(raw: list[dict]) -> list[dict]:
    """Normalise FF rows to {currency, impact, dt(UTC)} for high-impact only."""
    out: list[dict] = []
    for row in raw or []:
        if str(row.get("impact", "")).lower() not in ("high",):
            continue
        ts = row.get("date") or row.get("timestamp")
        dt = None
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        elif isinstance(ts, str):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                dt = None
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        out.append({"currency": str(row.get("country") or row.get("currency") or "").upper(),
                    "impact": "high", "dt": dt})
    return out


def _load_cached() -> list[dict] | None:
    # TTL keyed on the fetch time stored inside the file, not mtime — a
    # branch-restored cache has its mtime reset to "now" by git checkout, which
    # would freeze the calendar (same bug as the bar cache). Legacy bare-list
    # files have no fetch time -> treated as stale so the feed refreshes.
    p = _cache_path()
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if not isinstance(raw, dict) or "fetched_at" not in raw:
        return None
    if (time.time() - float(raw["fetched_at"])) >= _CACHE_TTL:
        return None
    return _parse_events(raw.get("raw", []))


def fetch_high_impact_events() -> list[dict] | None:
    """Return high-impact events, or None if the feed is unavailable (fail-open
    signal). Cached on disk for an hour."""
    cached = _load_cached()
    if cached is not None:
        return cached
    try:
        cfg.ensure_state_dirs()
        resp = httpx.get(cfg.FF_CALENDAR_URL, timeout=10.0)
        resp.raise_for_status()
        raw = resp.json()
        _cache_path().write_text(
            json.dumps({"fetched_at": time.time(), "raw": raw}), encoding="utf-8")
        return _parse_events(raw)
    except Exception as e:  # noqa: BLE001 — any failure => fail open
        log.warning("news calendar unavailable (%s) — failing open (no block)", e)
        return None


def news_blackout(symbol: str, now: datetime | None = None,
                  events: list[dict] | None = None) -> tuple[bool, str]:
    """(blocked, reason). Blocked if `now` is within +/- FX_NEWS_WINDOW_MIN of a
    high-impact event for either of the pair's currencies. Fail-open on no feed."""
    if not cfg.FX_NEWS_FILTER:
        return False, "news filter off"
    now = now or datetime.now(timezone.utc)
    if events is None:
        events = fetch_high_impact_events()
    if events is None:
        return False, "news feed unavailable — failed open"
    base, quote = _ccy(symbol)
    window = timedelta(minutes=cfg.FX_NEWS_WINDOW_MIN)
    for ev in events:
        if ev["currency"] not in (base, quote):
            continue
        if abs(ev["dt"] - now) <= window:
            return True, (f"within {cfg.FX_NEWS_WINDOW_MIN}m of high-impact "
                          f"{ev['currency']} event at {ev['dt'].isoformat()}")
    return False, "no nearby high-impact event"
