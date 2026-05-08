"""Market data: Massive (Polygon) for daily candles + news, yfinance for intraday.

Massive free Stocks Basic returns ~250 daily bars per request reliably, but
its intraday aggregates (1H/4H/15M) come back empty. yfinance fills the
intraday gap on a small whitelisted set of tickers (see YAHOO_TICKERS in
config.py). 4H bars are synthesised locally by grouping 1H bars.

Massive is rate-limited at 5/min on free; yfinance has no documented limit
but Yahoo throttles aggressive scraping — we make at most one call per
symbol per scan and don't retry on transient failure.
"""
import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from config import (
    INTRADAY_LOOKBACK_DAYS,
    MASSIVE_API_KEY,
    MASSIVE_BASE_URL,
    MASSIVE_RATE_LIMIT_PER_MIN,
    MASSIVE_RETRY_MAX,
    TIMEFRAMES,
    WATCHLIST,
    YAHOO_TICKERS,
)

log = logging.getLogger(__name__)


@dataclass
class Bar:
    t: int       # unix ms (start of bar)
    o: float
    h: float
    l: float
    c: float
    v: float

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.t / 1000, tz=timezone.utc)


class _RateLimiter:
    def __init__(self, max_requests: int, period_seconds: float):
        self.max = max_requests
        self.period = period_seconds
        self.timestamps: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            now = time.monotonic()
            while self.timestamps and self.timestamps[0] <= now - self.period:
                self.timestamps.popleft()
            if len(self.timestamps) >= self.max:
                wait = self.timestamps[0] + self.period - now
                if wait > 0:
                    log.debug("Rate limiter sleeping %.1fs", wait)
                    await asyncio.sleep(wait)
            self.timestamps.append(time.monotonic())


_limiter = _RateLimiter(MASSIVE_RATE_LIMIT_PER_MIN, 60.0)


def to_polygon_ticker(symbol: str) -> str:
    return WATCHLIST.get(symbol, symbol.upper())


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, str]) -> dict[str, Any] | None:
    """GET with rate limiting and exponential backoff. Returns None on terminal failure."""
    for attempt in range(MASSIVE_RETRY_MAX + 1):
        await _limiter.acquire()
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as e:
            log.warning("Network error (attempt %d): %s", attempt + 1, e)
            if attempt < MASSIVE_RETRY_MAX:
                await asyncio.sleep(2 ** attempt)
                continue
            return None
        if resp.status_code == 429:
            wait = 2 ** attempt
            log.warning("429 rate-limited, waiting %ds (attempt %d)", wait, attempt + 1)
            await asyncio.sleep(wait)
            continue
        if resp.status_code >= 500:
            wait = 2 ** attempt
            log.warning("HTTP %d, retrying in %ds", resp.status_code, wait)
            await asyncio.sleep(wait)
            continue
        if resp.status_code >= 400:
            log.error("HTTP %d for %s: %s", resp.status_code, url, resp.text[:200])
            return None
        try:
            return resp.json()
        except ValueError as e:
            log.error("Bad JSON from %s: %s", url, e)
            return None
    return None


def _bars_window(timespan: str, multiplier: int, count: int) -> tuple[str, str]:
    """Compute a generous from/to window. We over-fetch and let the API cap to `limit`."""
    now = datetime.now(timezone.utc)
    if timespan == "day":
        # Polygon Stocks Basic (free) docs claim 5 years of EOD history. Ask
        # for 8 years so we get whatever the API is willing to return — the
        # API silently truncates to its tier-cap.
        start = now - timedelta(days=8 * 365)
    else:
        # Intraday is not supported on free tier — kept for completeness but
        # callers shouldn't hit this path.
        start = now - timedelta(days=180)
    return start.date().isoformat(), now.date().isoformat()


async def fetch_candles(
    client: httpx.AsyncClient,
    symbol: str,
    timeframe: str,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    debug: bool = False,
) -> list[Bar]:
    """Fetch OHLCV bars for a symbol on a timeframe. Returns [] on failure."""
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unknown timeframe: {timeframe}")
    cfg = TIMEFRAMES[timeframe]
    ticker = to_polygon_ticker(symbol)
    multiplier = cfg["multiplier"]
    timespan = cfg["timespan"]
    bar_count = cfg["bars"]
    auto_frm, auto_to = _bars_window(timespan, multiplier, bar_count)
    frm = from_date or auto_frm
    to = to_date or auto_to
    url = f"{MASSIVE_BASE_URL}/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{frm}/{to}"
    params = {
        "adjusted": "true",
        "sort": "asc",
        # Polygon's per-request hard cap is 50000 — well above any window
        # the free tier will actually serve, so we just ask for everything.
        "limit": "50000",
        "apiKey": MASSIVE_API_KEY,
    }

    if debug:
        safe_params = {**params, "apiKey": "***"}
        qs = "&".join(f"{k}={v}" for k, v in safe_params.items())
        log.info("FETCH %s [%s] %s?%s", symbol, timeframe, url, qs)

    data = await _get_json(client, url, params)
    if not data or not data.get("results"):
        envelope = (
            {k: v for k, v in data.items() if k != "results"}
            if isinstance(data, dict) else data
        )
        log.warning(
            "No candles for %s [%s] (ticker=%s, window=%s..%s) — response envelope: %s",
            symbol, timeframe, ticker, frm, to, envelope,
        )
        return []
    bars = [
        Bar(
            t=item["t"],
            o=float(item["o"]),
            h=float(item["h"]),
            l=float(item["l"]),
            c=float(item["c"]),
            v=float(item.get("v", 0)),
        )
        for item in data["results"]
    ]
    if debug:
        log.info(
            "FETCH %s [%s] OK — %d bars (resultsCount=%s queryCount=%s status=%s)",
            symbol, timeframe, len(bars),
            data.get("resultsCount"), data.get("queryCount"), data.get("status"),
        )
    return bars[-bar_count:]


async def fetch_daily(client: httpx.AsyncClient, symbol: str) -> list[Bar]:
    """Convenience: fetch the daily bars for one symbol."""
    return await fetch_candles(client, symbol, "D")


def to_yahoo_ticker(symbol: str) -> str | None:
    """Yahoo ticker for a watchlist symbol, or None if unmapped."""
    return YAHOO_TICKERS.get(symbol)


def _fetch_yf_hourly_sync(symbol: str, lookback_days: int) -> list[Bar]:
    """Blocking call into yfinance. Always wrap in `asyncio.to_thread` from
    async code so we don't stall the event loop."""
    yticker = to_yahoo_ticker(symbol)
    if yticker is None:
        log.debug("No Yahoo ticker mapping for %s — skipping intraday", symbol)
        return []
    try:
        # Imported lazily so the rest of the codebase doesn't pay the
        # pandas/numpy import tax on processes that don't need intraday.
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed — run `pip install yfinance`")
        return []

    try:
        ticker = yf.Ticker(yticker)
        df = ticker.history(
            period=f"{lookback_days}d",
            interval="1h",
            auto_adjust=False,
            prepost=False,
            actions=False,
        )
    except Exception as e:  # yfinance raises a wide variety of errors
        log.warning("yfinance fetch failed for %s (%s): %s", symbol, yticker, e)
        return []

    if df is None or df.empty:
        log.warning("No yfinance 1H data for %s (%s)", symbol, yticker)
        return []

    bars: list[Bar] = []
    for ts, row in df.iterrows():
        try:
            t_ms = int(ts.timestamp() * 1000)
            bars.append(Bar(
                t=t_ms,
                o=float(row["Open"]),
                h=float(row["High"]),
                l=float(row["Low"]),
                c=float(row["Close"]),
                v=float(row.get("Volume", 0)),
            ))
        except (KeyError, TypeError, ValueError) as e:
            log.debug("Skipping malformed yfinance row for %s: %s", symbol, e)
            continue
    return bars


async def fetch_yf_hourly(symbol: str, lookback_days: int = INTRADAY_LOOKBACK_DAYS) -> list[Bar]:
    """Fetch 1H bars from Yahoo Finance for one watchlist symbol.

    Returns [] if the symbol has no Yahoo mapping or if yfinance fails —
    callers should treat empty as "no intraday signal" rather than an error.
    """
    return await asyncio.to_thread(_fetch_yf_hourly_sync, symbol, lookback_days)


def _fetch_live_price_sync(symbol: str) -> float | None:
    """Latest live-ish price from yfinance, on the SAME instrument the daily
    detector is using (Polygon/Massive ticker, e.g. USO for USOIL — NOT the
    CL=F front-month future used for intraday).

    Keeping this on the same ticker as `fetch_daily` is critical for the
    staleness check: the OB zones come from USO daily bars, so the live
    price must also be in USO terms or the 2% threshold compares apples
    to oranges and false-invalidates every USOIL setup.

    Tries fast_info.last_price first; falls back to the most recent 1m close.
    Returns None on any failure — caller should fall back further."""
    yticker = to_polygon_ticker(symbol)  # USO / NVDA / TSLA — yfinance handles all of these
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        ticker = yf.Ticker(yticker)
        try:
            fi = ticker.fast_info
            price = fi["last_price"] if "last_price" in fi else getattr(fi, "last_price", None)
            if price is not None:
                p = float(price)
                if p > 0:
                    return p
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            log.debug("fast_info miss for %s: %s", yticker, e)
        df = ticker.history(
            period="1d", interval="1m",
            auto_adjust=False, prepost=False, actions=False,
        )
        if df is not None and not df.empty:
            return float(df["Close"].iloc[-1])
    except Exception as e:
        log.warning("Live price fetch failed for %s (%s): %s", symbol, yticker, e)
    return None


async def fetch_live_price(symbol: str) -> float | None:
    """Async wrapper. Returns None on any failure — caller should fall back."""
    return await asyncio.to_thread(_fetch_live_price_sync, symbol)


def build_synthetic_4h(hourly_candles: list[Bar]) -> list[Bar]:
    """Group every 4 consecutive 1H candles into one 4H candle.

    Aggregation: first open, max high, min low, last close, summed volume.
    Right-aligned grouping so the latest synthetic bar always contains the
    latest 1H bar; the oldest partial group (<4 bars) is dropped.
    """
    if len(hourly_candles) < 4:
        return []
    n_full = len(hourly_candles) // 4
    keep = hourly_candles[-(n_full * 4):]
    out: list[Bar] = []
    for i in range(0, len(keep), 4):
        group = keep[i:i + 4]
        out.append(Bar(
            t=group[0].t,
            o=group[0].o,
            h=max(b.h for b in group),
            l=min(b.l for b in group),
            c=group[-1].c,
            v=sum(b.v for b in group),
        ))
    return out


async def fetch_news(client: httpx.AsyncClient, symbol: str, limit: int = 3) -> list[dict[str, str]]:
    ticker = to_polygon_ticker(symbol)
    url = f"{MASSIVE_BASE_URL}/v2/reference/news"
    params = {
        "ticker": ticker,
        "limit": str(limit),
        "order": "desc",
        "sort": "published_utc",
        "apiKey": MASSIVE_API_KEY,
    }
    data = await _get_json(client, url, params)
    if not data:
        return []
    results = data.get("results", []) or []
    return [
        {
            "title": item.get("title", ""),
            "publisher": (item.get("publisher") or {}).get("name", ""),
            "published": item.get("published_utc", ""),
            "description": (item.get("description") or "")[:300],
        }
        for item in results[:limit]
    ]
