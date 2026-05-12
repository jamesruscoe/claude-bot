"""FastAPI dashboard server.

Run: `py dashboard.py`
Then open http://localhost:8000/dashboard

Listens for internal pushes from scan.py and watch.py at /internal/*. Falls
back to JSON files on disk for state, so the dashboard survives the scanner
restarting.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import memory
import paper_trader
from config import (
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    SCAN_RESULTS_FILE,
    STATIC_DIR,
    WATCHING_STATE_FILE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(title="Trading Bot Dashboard", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ---------- SSE fan-out ----------
_subscribers: set[asyncio.Queue] = set()
_subscribers_lock = asyncio.Lock()


async def _broadcast(event_type: str, data: Any) -> None:
    payload = {"type": event_type, "data": data}
    async with _subscribers_lock:
        targets = list(_subscribers)
    for q in targets:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning("Subscriber queue full, dropping event")


# ---------- File-backed state helpers ----------

def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read %s: %s", path, e)
        return default


def _read_scan_results() -> dict[str, Any]:
    return _read_json(SCAN_RESULTS_FILE, {"timestamp": None, "results": []})


def _read_watching_state() -> dict[str, Any]:
    return _read_json(WATCHING_STATE_FILE, {"watching": {}})


# ---------- Request models for /internal/* ----------

class InternalAlertPush(BaseModel):
    trade: dict[str, Any]


class InternalScanPush(BaseModel):
    timestamp: str
    results: list[dict[str, Any]]


class InternalWatchHeartbeat(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    state: Literal["started", "scanning", "stopped"] = "scanning"


class OutcomeRequest(BaseModel):
    trade_id: str = Field(min_length=1)
    outcome: Literal["win", "loss", "stopped"]


# ---------- Public routes ----------

@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard")
async def dashboard_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    scan = _read_scan_results()
    watching = _read_watching_state().get("watching", {})
    return {
        "status": "ok",
        "last_scan_at": scan.get("timestamp"),
        "scanned_symbols": len(scan.get("results", [])),
        "watching": list(watching.keys()),
        "total_trades": memory.total_count(),
    }


@app.get("/trades")
async def trades() -> list[dict[str, Any]]:
    return memory.get_recent_trades(limit=10)


@app.get("/scan_results")
async def scan_results() -> dict[str, Any]:
    return _read_scan_results()


def _paper_payload() -> dict[str, Any]:
    """Snapshot of paper-trading state for the dashboard. Open trades are
    annotated with current_price + unrealised_r using the live prices from
    the most recent scan, so the UI doesn't have to re-fetch."""
    scan = _read_scan_results()
    prices = {
        r["symbol"]: r["current_price"]
        for r in scan.get("results", []) or []
        if r.get("symbol") and r.get("current_price") is not None
    }
    open_trades = paper_trader.compute_unrealised(paper_trader.list_open(), prices)
    closed_trades = paper_trader.list_closed()
    # Newest closes first — dashboard renders the table top-down.
    closed_trades.sort(key=lambda t: t.get("closed_at") or "", reverse=True)
    # Per-symbol breakdown for the win-rate bar chart.
    per_symbol: dict[str, dict[str, Any]] = {}
    for t in closed_trades:
        sym = t.get("symbol") or "?"
        if sym not in per_symbol:
            per_symbol[sym] = paper_trader.get_symbol_stats(sym)
    return {
        "open": open_trades,
        "closed": closed_trades,
        "stats": paper_trader.get_system_stats(),
        "per_symbol": list(per_symbol.values()),
    }


@app.get("/paper_trades")
async def paper_trades() -> dict[str, Any]:
    return _paper_payload()


@app.get("/watching")
async def watching_state() -> dict[str, Any]:
    return _read_watching_state()


@app.post("/outcome")
async def outcome(req: OutcomeRequest) -> dict[str, Any]:
    updated = memory.update_outcome(req.trade_id, req.outcome)
    if updated is None:
        raise HTTPException(status_code=404, detail="trade not found")
    await _broadcast("outcome_updated", updated)
    return updated


# ---------- Internal push routes (used by scan.py and watch.py) ----------

@app.post("/internal/alert")
async def internal_alert(req: InternalAlertPush) -> dict[str, str]:
    await _broadcast("alert", req.trade)
    return {"ok": "true"}


@app.post("/internal/scan_complete")
async def internal_scan_complete(req: InternalScanPush) -> dict[str, str]:
    await _broadcast("scan_complete", {"timestamp": req.timestamp, "results": req.results})
    # Paper-trading state is updated by scan.py before push_scan_complete
    # runs, so we can broadcast the fresh snapshot in the same event tick.
    await _broadcast("paper_update", _paper_payload())
    return {"ok": "true"}


@app.post("/internal/watching")
async def internal_watching(req: InternalWatchHeartbeat) -> dict[str, str]:
    state = _read_watching_state()
    watching = state.get("watching", {})
    if req.state == "stopped":
        watching.pop(req.symbol, None)
    else:
        watching[req.symbol] = {
            "last_seen": datetime.now(timezone.utc).isoformat(),
            "state": req.state,
        }
    state["watching"] = watching
    tmp = WATCHING_STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(WATCHING_STATE_FILE)
    await _broadcast("watching_changed", state)
    return {"ok": "true"}


# ---------- SSE stream ----------

@app.get("/events")
async def events(request: Request) -> EventSourceResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    async with _subscribers_lock:
        _subscribers.add(queue)

    async def stream() -> AsyncIterator[dict[str, str]]:
        try:
            yield {"event": "hello", "data": json.dumps({"connected": True})}
            # Send the current state on connect so the page populates immediately
            yield {"event": "scan_complete", "data": json.dumps(_read_scan_results(), default=str)}
            yield {"event": "watching_changed", "data": json.dumps(_read_watching_state(), default=str)}
            yield {"event": "paper_update", "data": json.dumps(_paper_payload(), default=str)}

            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield {
                        "event": payload["type"],
                        "data": json.dumps(payload["data"], default=str),
                    }
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            async with _subscribers_lock:
                _subscribers.discard(queue)

    return EventSourceResponse(stream())


# ---------- Pushable client (used by scan.py and watch.py) ----------

async def push_alert(trade: dict[str, Any]) -> None:
    """Best-effort push to a running dashboard. Silent if not reachable."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            await c.post(f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/internal/alert",
                         json={"trade": trade})
    except (httpx.HTTPError, OSError):
        pass


async def push_scan_complete(timestamp: str, results: list[dict[str, Any]]) -> None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            await c.post(f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/internal/scan_complete",
                         json={"timestamp": timestamp, "results": results})
    except (httpx.HTTPError, OSError):
        pass


async def push_watching(symbol: str, state: str = "scanning") -> None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            await c.post(f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/internal/watching",
                         json={"symbol": symbol, "state": state})
    except (httpx.HTTPError, OSError):
        pass


# ---------- Entry point ----------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard:app", host=DASHBOARD_HOST, port=DASHBOARD_PORT, reload=False)
