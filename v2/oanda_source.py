"""OANDA v20 practice DataSource adapter — REAL bid/ask candles, DATA ONLY.

This is the "clean data" feed scoped in OANDA_ADAPTER_SCOPE.md. It plugs in
behind the existing `datasource.DataSource` interface so the detectors, levels,
resolution and replay harness run against it unchanged. Two things make it worth
the trouble over the yfinance feed:

  • Real opens. Yahoo's daily opens were degenerate (~0.01% median move), which
    turned out to manufacture most of the apparent edge. OANDA candles carry the
    true session open, so the close-to-close vs open-to-close question dissolves.
  • Real bid/ask. Candles are requested at `price=BAM` (bid, ask, mid), so
    `spread_pips` returns the MEASURED spread instead of the assumed per-pair
    constant — entry, R:R and cost are net of the real quoted spread.

SCOPE GUARD: this module touches ONLY the candles/pricing DATA endpoints of the
v20 REST API. It does not import, construct, or reference any orders / trades /
positions endpoint. There is no live-order path here and none is to be added in
this phase. Practice host only.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

import httpx

from market_data import Bar
from v2 import config as cfg

log = logging.getLogger(__name__)

_RETRIES = 3
# Long cache: historical daily candles up to yesterday are immutable, so a pull
# is reusable for a day. Keyed on fetched_at inside the file (same fix as the FX
# cache — mtime is unreliable once CI restores state from a branch).
OANDA_CACHE_TTL_SECONDS = int(__import__("os").getenv("BOT_OANDA_CACHE_TTL", "86400"))


def _parse_oanda_time(s: str) -> int:
    """OANDA RFC3339 like '2020-01-01T22:00:00.000000000Z' -> epoch ms.

    fromisoformat chokes on 9-digit (nanosecond) fractions, so truncate to whole
    seconds — daily/H1 bars don't need sub-second precision."""
    s = s.rstrip("Z")
    if "." in s:
        s = s.split(".", 1)[0]
    dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class OANDASource:
    """OANDA v20 practice adapter. Daily bars built from MID OHLC; the measured
    bid/ask spread (in pips) is recorded per bar so the levels layer can charge
    the real cost instead of an assumed constant."""

    name = "fx_oanda"
    intraday_supported = True  # H1/M1 available; Phase A resolves on daily only

    def __init__(self, *, token: str | None = None, host: str | None = None) -> None:
        self._token = token if token is not None else cfg.OANDA_API_TOKEN
        self._host = host if host is not None else cfg.OANDA_HOST
        self._client: httpx.AsyncClient | None = None
        # symbol -> {t_ms: spread_pips} recorded during fetch_daily
        self._spread_series: dict[str, dict[int, float]] = {}

    # ---- interface ------------------------------------------------------ #

    def symbols(self) -> list[str]:
        return list(cfg.FX_BASKET)

    def pip_size(self, symbol: str) -> float | None:
        return cfg.fx_pip_size(symbol)

    def spread_pips(self, symbol: str) -> float:
        """MEASURED median spread (pips) if we've fetched this symbol, else the
        assumed conservative constant as a fallback."""
        series = self._spread_series.get(symbol)
        if series:
            return round(median(series.values()), 3)
        return cfg.fx_spread_pips(symbol)

    def measured_spread_stats(self, symbol: str,
                              bars: list[Bar] | None = None) -> dict[str, float] | None:
        """median / p90 / n of the measured spread (pips). If `bars` is given,
        restrict to those timestamps (e.g. the TRAIN window)."""
        series = self._spread_series.get(symbol)
        if not series:
            return None
        if bars is not None:
            keys = {b.t for b in bars}
            vals = [v for t, v in series.items() if t in keys]
        else:
            vals = list(series.values())
        if not vals:
            return None
        vals.sort()
        p90 = vals[min(len(vals) - 1, int(0.9 * len(vals)))]
        return {"median": round(median(vals), 4), "p90": round(p90, 4),
                "n": len(vals)}

    # ---- http ----------------------------------------------------------- #

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            if not self._token:
                raise RuntimeError(
                    "OANDA_API_TOKEN not set — add it to env/.env (practice token). "
                    "Never hardcode or commit it.")
            self._client = httpx.AsyncClient(
                base_url=f"https://{self._host}",
                headers={"Authorization": f"Bearer {self._token}",
                         "Accept-Datetime-Format": "RFC3339"},
                timeout=30.0,
            )
        return self._client

    async def _get_candles_page(self, instrument: str, *, granularity: str,
                                start: str, count: int,
                                include_first: bool = False) -> list[dict]:
        """One candles request. DATA endpoint only: /v3/instruments/{i}/candles."""
        client = self._get_client()
        params = {
            "granularity": granularity,
            "price": "BAM",          # bid, ask, mid OHLC per candle
            "from": start,
            "count": count,
            # Only the very first request keeps its `from` candle; later pages set
            # `start` to the last bar we already have, so exclude it to avoid a dup.
            "includeFirst": "true" if include_first else "false",
            "smooth": "false",
        }
        last_exc: Exception | None = None
        for attempt in range(_RETRIES):
            try:
                r = await client.get(
                    f"/v3/instruments/{instrument}/candles", params=params)
                if r.status_code == 400:
                    # usually "from is too far in the past" for a young instrument
                    log.debug("oanda 400 %s from=%s: %s", instrument, start, r.text[:200])
                    return []
                r.raise_for_status()
                return r.json().get("candles", [])
            except Exception as e:  # noqa: BLE001 — retry any transport/5xx
                last_exc = e
                log.debug("oanda %s %s attempt %d failed: %s",
                          instrument, granularity, attempt + 1, e)
                time.sleep(0.5 * (attempt + 1))
        if last_exc:
            log.warning("oanda %s %s gave up after %d tries: %s",
                        instrument, granularity, _RETRIES, last_exc)
        return []

    async def _fetch_all_candles(self, instrument: str, *, granularity: str,
                                 start_iso: str) -> list[dict]:
        """Paginate forward from `start_iso` to the present (v20 caps 5000/req)."""
        cursor = start_iso
        out: list[dict] = []
        seen_times: set[str] = set()
        for page_i in range(200):  # 200*5000 daily bars is centuries — safety only
            page = await self._get_candles_page(
                instrument, granularity=granularity, start=cursor,
                count=cfg.OANDA_MAX_CANDLES, include_first=(page_i == 0))
            complete = [c for c in page if c.get("complete")]
            fresh = [c for c in complete if c["time"] not in seen_times]
            # Terminate ONLY when a page yields no NEW candles — never on
            # len(page) < cap, which is unreliable (includeFirst drops one, so a
            # full-history first page returns cap-1 and would stop prematurely,
            # truncating the pull at ~5000 bars).
            if not fresh:
                break
            for c in fresh:
                seen_times.add(c["time"])
            out += fresh
            cursor = fresh[-1]["time"]
        return out

    # ---- caching -------------------------------------------------------- #

    def _cache_path(self, symbol: str, granularity: str) -> Path:
        safe = symbol.replace("=", "_").replace("/", "_")
        return cfg.CACHE_DIR / f"oanda_{safe}_{granularity}.json"

    def _read_cache(self, symbol: str, granularity: str) -> list[dict] | None:
        path = self._cache_path(symbol, granularity)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        if not isinstance(raw, dict) or "fetched_at" not in raw:
            return None
        if (time.time() - float(raw["fetched_at"])) > OANDA_CACHE_TTL_SECONDS:
            return None
        return raw.get("rows")

    def _write_cache(self, symbol: str, granularity: str, rows: list[dict]) -> None:
        cfg.ensure_state_dirs()
        try:
            self._cache_path(symbol, granularity).write_text(
                json.dumps({"fetched_at": time.time(), "rows": rows}),
                encoding="utf-8")
        except OSError as e:
            log.debug("oanda cache write failed for %s: %s", symbol, e)

    # ---- rows <-> bars -------------------------------------------------- #

    def _candles_to_rows(self, symbol: str, candles: list[dict]) -> list[dict]:
        """Flatten OANDA BAM candles to compact rows: mid OHLCV + spread pips."""
        pip = cfg.fx_pip_size(symbol)
        rows: list[dict] = []
        for c in candles:
            try:
                mid, bid, ask = c["mid"], c["bid"], c["ask"]
                sp = (float(ask["c"]) - float(bid["c"])) / pip
                rows.append({
                    "t": _parse_oanda_time(c["time"]),
                    "o": float(mid["o"]), "h": float(mid["h"]),
                    "l": float(mid["l"]), "c": float(mid["c"]),
                    "v": float(c.get("volume", 0) or 0),
                    "sp": round(sp, 4),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return [r for r in rows if r["h"] >= r["l"] and r["c"] > 0]

    def _rows_to_bars(self, symbol: str, rows: list[dict]) -> list[Bar]:
        self._spread_series[symbol] = {r["t"]: r["sp"] for r in rows}
        return [Bar(t=r["t"], o=r["o"], h=r["h"], l=r["l"], c=r["c"], v=r["v"])
                for r in rows]

    # ---- DataSource methods -------------------------------------------- #

    async def fetch_daily(self, symbol: str) -> list[Bar]:
        instrument = cfg.OANDA_INSTRUMENTS.get(symbol)
        if instrument is None:
            log.warning("no OANDA instrument mapping for %s", symbol)
            return []
        rows = self._read_cache(symbol, "D")
        if rows is None:
            start = f"{cfg.OANDA_CANDLE_START}T00:00:00Z"
            candles = await self._fetch_all_candles(
                instrument, granularity="D", start_iso=start)
            rows = self._candles_to_rows(symbol, candles)
            if rows:
                self._write_cache(symbol, "D", rows)
        return self._rows_to_bars(symbol, rows or [])

    async def resolution_bars(self, symbol: str, since: datetime) -> list[Bar]:
        """H1 candles since `since`, for honest intrabar resolution. Not used by
        the Phase A daily baseline (which resolves against forward daily bars);
        implemented so the live pipeline can use this source later."""
        instrument = cfg.OANDA_INSTRUMENTS.get(symbol)
        if instrument is None:
            return []
        start = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        candles = await self._fetch_all_candles(
            instrument, granularity="H1", start_iso=start)
        rows = self._candles_to_rows(symbol, candles)
        bars = [Bar(t=r["t"], o=r["o"], h=r["h"], l=r["l"], c=r["c"], v=r["v"])
                for r in rows]
        return [b for b in bars if b.dt > since]

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
