"""Data-source interface + adapters.

The pipeline talks to ONE interface so a different feed (OANDA practice, a paid
vendor) can slot in later without touching strategy code. Two adapters today:

  • EquitiesSource — wraps the existing Massive/yfinance `market_data` module.
    Daily EOD only (Massive free tier), so trade resolution falls back to the
    daily bar's high/low. Behaviour is unchanged from the original v2.

  • FXSource — yfinance FX adapter. Daily OHLC for the basket, plus intraday
    (1m ≈ 7d, hourly ≈ 60d) used for HONEST, intrabar SL/TP-first resolution.
    yfinance is unofficial and delayed, so every pull is retried, cached, and
    guarded: an empty or stale response makes the caller skip rather than act
    on bad data.

Risk-math metadata (pip size, assumed spread) lives on the source so the levels
layer stays instrument-agnostic. Equities returns pip_size=None to signal
"price-based, not pip-based".
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import httpx

import market_data
from market_data import Bar
from v2 import config as cfg

log = logging.getLogger(__name__)


class DataSource(Protocol):
    name: str
    intraday_supported: bool

    def symbols(self) -> list[str]: ...
    async def fetch_daily(self, symbol: str) -> list[Bar]: ...
    async def resolution_bars(self, symbol: str, since: datetime) -> list[Bar]: ...
    def pip_size(self, symbol: str) -> float | None: ...
    def spread_pips(self, symbol: str) -> float: ...


# --------------------------------------------------------------------------- #
# Equities (unchanged behaviour)                                              #
# --------------------------------------------------------------------------- #

class EquitiesSource:
    """Adapter over the original Massive-backed `market_data`. Daily only; trade
    resolution uses the latest daily bar's high/low (still honest vs. the old
    close-only bug, just coarser than FX intraday)."""

    name = "equities"
    intraday_supported = False

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def symbols(self) -> list[str]:
        return list(cfg.WATCHLIST.keys())

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def fetch_daily(self, symbol: str) -> list[Bar]:
        client = await self._get_client()
        try:
            return await market_data.fetch_daily(client, symbol)
        except Exception as e:  # noqa: BLE001 — never let one symbol kill a scan
            log.warning("equities fetch failed for %s: %s", symbol, e)
            return []

    async def resolution_bars(self, symbol: str, since: datetime) -> list[Bar]:
        """No intraday on the free tier — resolve against recent daily bars."""
        bars = await self.fetch_daily(symbol)
        return [b for b in bars if b.dt > since]

    def pip_size(self, symbol: str) -> float | None:
        return None  # equities are price-based, not pip-based

    def spread_pips(self, symbol: str) -> float:
        return 0.0

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# --------------------------------------------------------------------------- #
# FX (yfinance)                                                               #
# --------------------------------------------------------------------------- #

_RETRIES = 3


def _df_to_bars(df) -> list[Bar]:
    if df is None or getattr(df, "empty", True):
        return []
    out: list[Bar] = []
    for ts, row in df.iterrows():
        try:
            out.append(Bar(
                t=int(ts.timestamp() * 1000),
                o=float(row["Open"]), h=float(row["High"]),
                l=float(row["Low"]), c=float(row["Close"]),
                v=float(row.get("Volume", 0) or 0),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    # yfinance can emit trailing NaN rows for the still-forming bar; drop them
    return [b for b in out if b.h >= b.l and b.c > 0]


class FXSource:
    """yfinance FX adapter with retry, on-disk daily cache, and a stale guard."""

    name = "fx"
    intraday_supported = True

    def symbols(self) -> list[str]:
        return list(cfg.FX_BASKET)

    def pip_size(self, symbol: str) -> float | None:
        return cfg.fx_pip_size(symbol)

    def spread_pips(self, symbol: str) -> float:
        return cfg.fx_spread_pips(symbol)

    # ---- caching -------------------------------------------------------- #

    def _cache_path(self, symbol: str, interval: str) -> Path:
        safe = symbol.replace("=", "_").replace("/", "_")
        return cfg.CACHE_DIR / f"{safe}_{interval}.json"

    def _read_cache(self, symbol: str, interval: str, ttl: int) -> list[Bar] | None:
        path = self._cache_path(symbol, interval)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        # TTL is keyed on the fetch time recorded INSIDE the file, never the
        # file's mtime: when CI restores state from the `state-fx` branch, git
        # sets every checked-out file's mtime to "now", which made an mtime-based
        # TTL look permanently fresh and froze the bars indefinitely. A legacy
        # bare-list cache has no fetch time -> treat as expired and refetch.
        if not isinstance(raw, dict) or "fetched_at" not in raw:
            return None
        if (time.time() - float(raw["fetched_at"])) > ttl:
            return None
        try:
            return [Bar(**b) for b in raw.get("bars", [])]
        except (TypeError, ValueError):
            return None

    def _write_cache(self, symbol: str, interval: str, bars: list[Bar]) -> None:
        cfg.ensure_state_dirs()
        try:
            self._cache_path(symbol, interval).write_text(
                json.dumps({"fetched_at": time.time(),
                            "bars": [b.__dict__ for b in bars]}),
                encoding="utf-8")
        except OSError as e:
            log.debug("cache write failed for %s: %s", symbol, e)

    # ---- fetch ---------------------------------------------------------- #

    def _yf_history(self, symbol: str, *, period: str, interval: str) -> list[Bar]:
        try:
            import yfinance as yf
        except ImportError:
            log.warning("yfinance not installed — `pip install yfinance`")
            return []
        last_exc: Exception | None = None
        for attempt in range(_RETRIES):
            try:
                df = yf.Ticker(symbol).history(
                    period=period, interval=interval,
                    auto_adjust=False, actions=False,
                )
                bars = _df_to_bars(df)
                if bars:
                    return bars
            except Exception as e:  # noqa: BLE001 — yfinance raises broadly
                last_exc = e
                log.debug("yf %s %s/%s attempt %d failed: %s",
                          symbol, period, interval, attempt + 1, e)
            time.sleep(0.4 * (attempt + 1))
        if last_exc:
            log.warning("yf %s %s/%s gave up after %d tries: %s",
                        symbol, period, interval, _RETRIES, last_exc)
        return []

    async def fetch_daily(self, symbol: str) -> list[Bar]:
        import asyncio
        cached = self._read_cache(symbol, "1d", cfg.FX_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached
        await market_data.yf_throttle()
        bars = await asyncio.to_thread(
            self._yf_history, symbol, period="3y", interval="1d")
        if bars:
            self._write_cache(symbol, "1d", bars)
        return bars

    async def resolution_bars(self, symbol: str, since: datetime) -> list[Bar]:
        """Intraday bars for honest, intrabar resolution. 1m has only ~7d of
        history, hourly ~60d — pick by how long the trade has been open."""
        import asyncio
        age_days = (datetime.now(timezone.utc) - since).days
        period, interval = ("7d", "1m") if age_days <= 6 else ("60d", "1h")
        await market_data.yf_throttle()
        bars = await asyncio.to_thread(
            self._yf_history, symbol, period=period, interval=interval)
        return [b for b in bars if b.dt > since]


# --------------------------------------------------------------------------- #

def get_data_source() -> DataSource:
    """Return the configured data source. Equities is the default (safe) path."""
    if cfg.FX_OANDA:
        from v2.oanda_source import OANDASource
        log.info("data source: FX (OANDA v20 practice), %d pairs", len(cfg.FX_BASKET))
        return OANDASource()
    if cfg.FX_ENABLED:
        log.info("data source: FX (yfinance), %d pairs", len(cfg.FX_BASKET))
        return FXSource()
    return EquitiesSource()
