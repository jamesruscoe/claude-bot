"""News and macro-event enrichment. No AI calls — purely deterministic."""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any

import httpx

from market_data import fetch_news

log = logging.getLogger(__name__)

# 2026 FOMC meeting dates (Tuesday + Wednesday). Update annually.
FOMC_2026_DATES = [
    (2026, 1, 27, 28),
    (2026, 3, 17, 18),
    (2026, 4, 28, 29),
    (2026, 6, 16, 17),
    (2026, 7, 28, 29),
    (2026, 9, 15, 16),
    (2026, 11, 3, 4),
    (2026, 12, 15, 16),
]


def _next_weekday(start: datetime, weekday: int) -> datetime:
    days_ahead = (weekday - start.weekday()) % 7
    return start + timedelta(days=days_ahead)


def _is_fomc_window(now: datetime, hours_ahead: int) -> tuple[bool, datetime | None]:
    horizon = now + timedelta(hours=hours_ahead)
    for year, month, day1, day2 in FOMC_2026_DATES:
        meeting_end = datetime(year, month, day2, 19, 0, tzinfo=timezone.utc)
        meeting_start = datetime(year, month, day1, 0, 0, tzinfo=timezone.utc)
        if meeting_start <= horizon and now <= meeting_end:
            return True, meeting_end
    return False, None


def upcoming_macro_events(now: datetime | None = None, hours_ahead: int = 24) -> list[dict[str, str]]:
    """Hardcoded high-impact events landing within `hours_ahead` of `now`."""
    if now is None:
        now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours_ahead)
    events: list[dict[str, str]] = []

    # EIA crude oil inventory: Wednesdays 15:30 UTC
    next_wed = _next_weekday(now.replace(hour=0, minute=0, second=0, microsecond=0), 2)
    eia_dt = next_wed.replace(hour=15, minute=30)
    if now <= eia_dt <= horizon:
        events.append({
            "event": "US Crude Oil Inventory (EIA)",
            "time": eia_dt.isoformat(),
            "impact": "high",
            "affects": "USOIL, oil-related assets",
        })

    # NFP: First Friday of the month, 13:30 UTC
    for offset in (0, 1):
        candidate = (now.replace(day=1) + timedelta(days=32 * offset)).replace(day=1)
        first_friday = _next_weekday(candidate, 4)
        nfp_dt = first_friday.replace(hour=13, minute=30, second=0, microsecond=0, tzinfo=timezone.utc)
        if now <= nfp_dt <= horizon:
            events.append({
                "event": "US Non-Farm Payrolls (NFP)",
                "time": nfp_dt.isoformat(),
                "impact": "high",
                "affects": "USD pairs, indices, gold",
            })

    is_fomc, fomc_end = _is_fomc_window(now, hours_ahead)
    if is_fomc and fomc_end is not None:
        events.append({
            "event": "FOMC Meeting Window",
            "time": fomc_end.isoformat(),
            "impact": "very high",
            "affects": "USD, indices, gold, all risk assets",
        })

    # UK market open: 08:00 UTC
    uk_open = datetime.combine(now.date(), time(8, 0), tzinfo=timezone.utc)
    if uk_open <= now:
        uk_open += timedelta(days=1)
    if uk_open <= horizon:
        events.append({
            "event": "UK Market Open (LSE)",
            "time": uk_open.isoformat(),
            "impact": "medium",
            "affects": "GBP pairs, FTSE, European indices",
        })

    # US market open: 13:30 UTC
    us_open = datetime.combine(now.date(), time(13, 30), tzinfo=timezone.utc)
    if us_open <= now:
        us_open += timedelta(days=1)
    if us_open <= horizon:
        events.append({
            "event": "US Market Open (NYSE)",
            "time": us_open.isoformat(),
            "impact": "medium",
            "affects": "US indices, USD pairs, equities",
        })

    return sorted(events, key=lambda e: e["time"])


def hours_until_next_high_impact(events: list[dict[str, str]], now: datetime | None = None) -> float | None:
    """Return hours until the next 'high' or 'very high' impact event, or None."""
    if now is None:
        now = datetime.now(timezone.utc)
    high_events = [e for e in events if e.get("impact") in ("high", "very high")]
    if not high_events:
        return None
    next_e = min(high_events, key=lambda e: e["time"])
    try:
        when = datetime.fromisoformat(next_e["time"].replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = (when - now).total_seconds() / 3600
    return delta if delta > 0 else None


async def enrich(client: httpx.AsyncClient, symbol: str) -> dict[str, Any]:
    """Fetch news + upcoming macro events for a symbol."""
    headlines = await fetch_news(client, symbol, limit=3)
    events = upcoming_macro_events(hours_ahead=24)
    return {"headlines": headlines, "upcoming_events": events}
