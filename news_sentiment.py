"""News sentiment + macro calendar layer.

Pulls ~10 headlines per symbol from `yfinance.Ticker(...).news` (no extra API),
scores each headline by keyword presence (+1 bullish, -1 bearish, 0 neutral),
and aggregates to an overall verdict:
    bullish   net score >= +2
    bearish   net score <= -2
    neutral   otherwise

Also exposes a hard-coded earnings calendar (TSLA, NVDA) and reuses
`enricher.upcoming_macro_events` for FOMC / NFP / EIA so the brief can warn
when a high-impact event is within 24 hours.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from market_data import to_yahoo_ticker

log = logging.getLogger(__name__)


# Match a whole token only — "lossless" must not trigger "loss".
BULLISH_KEYWORDS: set[str] = {
    "upgrade", "upgrades", "upgraded",
    "beat", "beats", "beating",
    "rally", "rallies", "rallying",
    "surge", "surges", "surging", "surged",
    "partnership", "partnerships",
    "deal", "deals",
    "growth", "growing",
    "record", "records",
    "outperform", "outperforms",
    "buy", "bullish",
    "raises", "raised",
    "approval", "approved",
    "soars", "soared",
    "jumps", "jumped",
}

BEARISH_KEYWORDS: set[str] = {
    "downgrade", "downgrades", "downgraded",
    "miss", "misses", "missed",
    "lawsuit", "lawsuits", "sued",
    "recall", "recalls", "recalled",
    "crash", "crashes", "crashing",
    "loss", "losses",
    "cuts", "cut",
    "warning", "warns", "warned",
    "investigation", "investigated",
    "probe", "probes",
    "fraud",
    "decline", "declines", "declining",
    "plunge", "plunges", "plunged",
    "bearish", "sell",
    "tumbles", "tumbled",
    "slump", "slumps", "slumped",
}

# Hard-coded next earnings dates (post-close US, in UTC). These are
# approximations — refresh after each release. Set to None to skip a symbol.
EARNINGS_DATES: dict[str, datetime] = {
    "NVDA": datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc),
    "TSLA": datetime(2026, 7, 22, 20, 0, tzinfo=timezone.utc),
}

_word_re = re.compile(r"[A-Za-z][A-Za-z'-]*")


# ---------- Headline scoring ----------

def score_headline(title: str) -> int:
    """+1 / 0 / -1 from keyword presence. Mixed signals → -1 (risk-off default)."""
    if not title:
        return 0
    words = {w.lower() for w in _word_re.findall(title)}
    bull = bool(words & BULLISH_KEYWORDS)
    bear = bool(words & BEARISH_KEYWORDS)
    if bull and not bear:
        return 1
    if bear and not bull:
        return -1
    if bull and bear:
        return -1
    return 0


def analyse_headlines(headlines: list[dict[str, Any]]) -> dict[str, Any]:
    """Score a list of headlines and return aggregate sentiment + top 3 most
    relevant. Relevance = absolute headline score, ties broken by recency."""
    scored: list[dict[str, Any]] = []
    for h in headlines:
        s = score_headline(h.get("title", ""))
        scored.append({**h, "score": s})

    net = sum(item["score"] for item in scored)
    if net >= 2:
        overall = "bullish"
    elif net <= -2:
        overall = "bearish"
    else:
        overall = "neutral"

    relevant = sorted(
        scored,
        key=lambda x: (abs(x["score"]), x.get("published", "")),
        reverse=True,
    )
    top = [s for s in relevant if s["score"] != 0][:3]
    if len(top) < 3:
        neutrals = sorted(
            (s for s in scored if s["score"] == 0),
            key=lambda x: x.get("published", ""),
            reverse=True,
        )
        top.extend(neutrals[: 3 - len(top)])

    return {
        "sentiment": overall,
        "score": net,
        "headline_count": len(scored),
        "top_headlines": top[:3],
    }


# ---------- yfinance news fetch ----------

def _normalise_yf_news_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """yfinance has shipped two shapes for `.news` — flat (older) and nested
    under `content` (newer). Normalise to {title, publisher, published, link}."""
    title = item.get("title")
    publisher = item.get("publisher", "") or ""
    link = item.get("link", "") or ""
    published_ts = item.get("providerPublishTime")

    content = item.get("content")
    if isinstance(content, dict):
        title = title or content.get("title")
        if not publisher:
            publisher = (content.get("provider") or {}).get("displayName", "") or ""
        if not link:
            link = (content.get("canonicalUrl") or {}).get("url", "") or ""
        pub_date = content.get("pubDate") or content.get("displayTime")
        if pub_date and not published_ts:
            try:
                published_ts = int(
                    datetime.fromisoformat(str(pub_date).replace("Z", "+00:00")).timestamp()
                )
            except (ValueError, AttributeError):
                pass

    if not title:
        return None

    published_iso = ""
    if isinstance(published_ts, (int, float)):
        try:
            published_iso = datetime.fromtimestamp(published_ts, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            published_iso = ""

    return {
        "title": str(title),
        "publisher": str(publisher),
        "published": published_iso,
        "link": str(link),
    }


def _fetch_news_sync(symbol: str, limit: int) -> list[dict[str, Any]]:
    yticker = to_yahoo_ticker(symbol)
    if yticker is None:
        log.debug("No Yahoo ticker mapping for %s — skipping news", symbol)
        return []
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed — `pip install yfinance`")
        return []
    try:
        raw = yf.Ticker(yticker).news or []
    except Exception as e:
        log.warning("yf news fetch failed for %s (%s): %s", symbol, yticker, e)
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:limit]:
        if not isinstance(item, dict):
            continue
        n = _normalise_yf_news_item(item)
        if n:
            out.append(n)
    return out


async def fetch_news(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    """Async wrapper around blocking yfinance.Ticker(symbol).news."""
    return await asyncio.to_thread(_fetch_news_sync, symbol, limit)


# ---------- Macro calendar ----------

def next_high_impact_event(
    symbol: str,
    *,
    now: datetime | None = None,
    hours: float = 24.0,
) -> dict[str, Any] | None:
    """Soonest earnings / FOMC / NFP / EIA event within `hours`. None if clear.

    Earnings dates live in EARNINGS_DATES here; FOMC/NFP/EIA are pulled from
    `enricher.upcoming_macro_events` so we don't duplicate hardcoded dates.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    candidates: list[dict[str, Any]] = []

    earn = EARNINGS_DATES.get(symbol)
    if earn:
        delta_h = (earn - now).total_seconds() / 3600
        if 0 <= delta_h <= hours:
            candidates.append({
                "event": f"{symbol} earnings",
                "time": earn.isoformat(),
                "impact": "very high",
                "hours_until": round(delta_h, 1),
            })

    # Local import — enricher is fine to import here, just avoiding a top-level
    # cycle if news_sentiment ever gets pulled into enricher.
    from enricher import upcoming_macro_events

    for ev in upcoming_macro_events(now=now, hours_ahead=int(hours) + 1):
        if ev.get("impact") not in ("high", "very high"):
            continue
        affects = (ev.get("affects") or "").upper()
        ev_name = (ev.get("event") or "").upper()
        relevant = (
            symbol.upper() in affects
            or (symbol == "USOIL" and ("OIL" in affects or "EIA" in ev_name))
            or (symbol in {"NVDA", "TSLA"} and ("INDICES" in affects or "USD" in affects))
        )
        if not relevant:
            continue
        try:
            when = datetime.fromisoformat(ev["time"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        delta_h = (when - now).total_seconds() / 3600
        if 0 <= delta_h <= hours:
            candidates.append({
                "event": ev["event"],
                "time": ev["time"],
                "impact": ev["impact"],
                "hours_until": round(delta_h, 1),
            })

    if not candidates:
        return None
    candidates.sort(key=lambda c: c["hours_until"])
    return candidates[0]


def macro_warning(symbol: str, *, now: datetime | None = None) -> str | None:
    """One-line warning if a high-impact event is within 24h, else None."""
    ev = next_high_impact_event(symbol, now=now, hours=24.0)
    if not ev:
        return None
    return (
        f"High impact event imminent ({ev['event']} in {ev['hours_until']:.1f}h) "
        "— reduce size or wait."
    )
