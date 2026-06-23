"""Market-calendar gate.

v1 fired signals on Juneteenth because the cron ran Mon-Fri with no holiday
awareness — it scanned a stale Thursday close and emailed a "TAKE TRADE" you
couldn't act on. This module is the fix: before any scan does work, we ask
whether the US market is actually open today (and whether the data we got is
fresh enough to trust).

Uses pandas-market-calendars when available; degrades to a hardcoded US
holiday list + weekday check if the library isn't installed, so the gate is
never silently bypassed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from v2.config import MARKET_CALENDAR, MAX_BAR_STALENESS_DAYS

log = logging.getLogger(__name__)


@dataclass
class GateResult:
    open: bool
    reason: str

    def __bool__(self) -> bool:  # `if gate:` reads naturally
        return self.open


# Fallback US market holidays (date-only, observed). Used only when
# pandas-market-calendars is missing. Covers the rolling window the bot cares
# about; extend yearly if you rely on the fallback long-term.
_FALLBACK_HOLIDAYS_2026 = {
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Jr. Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth  ← the v1 bug
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}


def _is_session_with_calendar(day: date) -> bool | None:
    """True/False from pandas-market-calendars, or None if unavailable."""
    try:
        import pandas_market_calendars as mcal
    except ImportError:
        return None
    try:
        cal = mcal.get_calendar(MARKET_CALENDAR)
        sched = cal.schedule(start_date=day.isoformat(), end_date=day.isoformat())
        return not sched.empty
    except Exception as e:  # pragma: no cover - defensive
        log.warning("market calendar lookup failed (%s); using fallback", e)
        return None


def is_trading_day(day: date | None = None) -> GateResult:
    """Is the US equity market open on `day` (default: today, UTC)?"""
    day = day or datetime.now(timezone.utc).date()
    if day.weekday() >= 5:
        return GateResult(False, f"{day} is a weekend")

    via_cal = _is_session_with_calendar(day)
    if via_cal is True:
        return GateResult(True, f"{day} is a trading session")
    if via_cal is False:
        return GateResult(False, f"{day} is a market holiday (exchange calendar)")

    # Fallback path — no calendar library installed.
    if day in _FALLBACK_HOLIDAYS_2026:
        return GateResult(False, f"{day} is a market holiday (fallback list)")
    return GateResult(True, f"{day} is a weekday (fallback — no exchange calendar)")


def bars_are_fresh(latest_bar_dt: datetime, *, now: datetime | None = None) -> GateResult:
    """Reject a scan whose freshest daily bar is too old to act on — e.g. the
    data feed didn't update, or we're running on a long-closed instrument."""
    now = now or datetime.now(timezone.utc)
    if latest_bar_dt.tzinfo is None:
        latest_bar_dt = latest_bar_dt.replace(tzinfo=timezone.utc)
    age_days = (now - latest_bar_dt).days
    if age_days > MAX_BAR_STALENESS_DAYS:
        return GateResult(
            False,
            f"freshest bar is {age_days}d old (> {MAX_BAR_STALENESS_DAYS}d) — stale feed",
        )
    return GateResult(True, f"freshest bar is {age_days}d old")
