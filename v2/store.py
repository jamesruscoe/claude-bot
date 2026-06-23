"""SQLite ledger — the hard source of truth.

Replaces v1's scatter of JSON files (paper_trades.json, fired_signals.json,
cooling_off.json, trades.json), each of which was wiped on every CI run
because GitHub Actions has an ephemeral filesystem. Here everything lives in
one SQLite file under STATE_DIR, which the workflow persists to the `state`
branch — so the bot accumulates a track record instead of starting blank
every few hours.

Tables
------
signals    every candidate the detector proposed (one row per symbol per scan)
decisions  the judge's take/skip verdict for a signal, with rationale
trades     positions opened from a "take" decision, resolved over time
lessons    structured pointers to distilled journal lessons (prose lives in md)

The storage layer is deliberately behind plain functions so swapping SQLite
for Turso/libSQL later is a one-file change.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

from v2 import config as cfg

log = logging.getLogger(__name__)

# Outcome vocabulary — ported from v1 paper_trader, which got this right.
OUTCOME_WIN_TP2 = "WIN_TP2"
OUTCOME_BREAKEVEN = "BREAKEVEN"   # TP1 hit, stop trailed to entry, then stopped
OUTCOME_LOSS = "LOSS"
OUTCOME_EXPIRED = "EXPIRED"
_WIN = {OUTCOME_WIN_TP2}
_LOSS = {OUTCOME_LOSS}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id           TEXT PRIMARY KEY,
    scan_ts      TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    score        INTEGER NOT NULL,
    direction    TEXT,
    setups       TEXT,            -- json list: ["ob_retest","bos_retest"]
    regime       TEXT,
    price        REAL,
    atr          REAL,
    entry_low    REAL,
    entry_high   REAL,
    stop_loss    REAL,
    tp1          REAL,
    tp2          REAL,
    rr           REAL
);
CREATE TABLE IF NOT EXISTS decisions (
    id           TEXT PRIMARY KEY,
    signal_id    TEXT NOT NULL,
    scan_ts      TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    take         INTEGER NOT NULL,   -- 0/1
    confidence   TEXT,               -- low|medium|high
    size         TEXT,               -- quarter|half|full|none
    rationale    TEXT,
    source       TEXT,               -- "claude:<model>" or "fallback"
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);
CREATE TABLE IF NOT EXISTS trades (
    id           TEXT PRIMARY KEY,
    signal_id    TEXT,
    symbol       TEXT NOT NULL,
    direction    TEXT NOT NULL,
    setups       TEXT,
    regime       TEXT,
    entry_price  REAL NOT NULL,
    stop_loss    REAL NOT NULL,
    original_sl  REAL NOT NULL,
    tp1          REAL NOT NULL,
    tp2          REAL NOT NULL,
    tp1_hit      INTEGER NOT NULL DEFAULT 0,
    tp1_hit_at   TEXT,
    opened_at    TEXT NOT NULL,
    closed_at    TEXT,
    close_price  REAL,
    outcome      TEXT,
    pnl_r        REAL,
    journaled    INTEGER NOT NULL DEFAULT 0   -- has reflection written a journal entry?
);
CREATE TABLE IF NOT EXISTS lessons (
    id           TEXT PRIMARY KEY,
    created_ts   TEXT NOT NULL,
    scope        TEXT,              -- symbol, setup name, "global", etc.
    summary      TEXT NOT NULL,
    journal_file TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_open ON trades(outcome);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
"""


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    cfg.ensure_state_dirs()
    conn = sqlite3.connect(cfg.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- writes ----------------------------------------------------------

def record_signal(scan_ts: str, candidate: dict[str, Any]) -> str:
    """Persist a detected candidate; returns its id."""
    sid = str(uuid.uuid4())
    with _conn() as c:
        c.execute(
            """INSERT INTO signals (id, scan_ts, symbol, score, direction, setups,
                regime, price, atr, entry_low, entry_high, stop_loss, tp1, tp2, rr)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sid, scan_ts, candidate["symbol"], candidate["score"],
                candidate.get("direction"), json.dumps(candidate.get("setups") or []),
                candidate.get("regime"), candidate.get("price"), candidate.get("atr"),
                candidate.get("entry_low"), candidate.get("entry_high"),
                candidate.get("stop_loss"), candidate.get("tp1"), candidate.get("tp2"),
                candidate.get("rr"),
            ),
        )
    return sid


def record_decision(signal_id: str, scan_ts: str, symbol: str, decision: dict[str, Any]) -> str:
    did = str(uuid.uuid4())
    with _conn() as c:
        c.execute(
            """INSERT INTO decisions (id, signal_id, scan_ts, symbol, take,
                confidence, size, rationale, source)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                did, signal_id, scan_ts, symbol, int(bool(decision.get("take"))),
                decision.get("confidence"), decision.get("size"),
                decision.get("rationale"), decision.get("source"),
            ),
        )
    return did


def open_trade(signal_id: str | None, candidate: dict[str, Any], fill: float,
               opened_at: str | None = None) -> dict[str, Any]:
    """Open a position. `fill` is the realistic entry (see levels.realistic_fill)."""
    tid = str(uuid.uuid4())
    opened_at = opened_at or _now()
    direction = candidate["direction"]
    sl = float(candidate["stop_loss"])
    row = {
        "id": tid, "signal_id": signal_id, "symbol": candidate["symbol"],
        "direction": direction, "setups": json.dumps(candidate.get("setups") or []),
        "regime": candidate.get("regime"), "entry_price": float(fill),
        "stop_loss": sl, "original_sl": sl,
        "tp1": float(candidate["tp1"]), "tp2": float(candidate["tp2"]),
        "tp1_hit": 0, "tp1_hit_at": None, "opened_at": opened_at,
        "closed_at": None, "close_price": None, "outcome": None,
        "pnl_r": None, "journaled": 0,
    }
    with _conn() as c:
        c.execute(
            """INSERT INTO trades (id, signal_id, symbol, direction, setups, regime,
                entry_price, stop_loss, original_sl, tp1, tp2, tp1_hit, tp1_hit_at,
                opened_at, closed_at, close_price, outcome, pnl_r, journaled)
               VALUES (:id,:signal_id,:symbol,:direction,:setups,:regime,:entry_price,
                :stop_loss,:original_sl,:tp1,:tp2,:tp1_hit,:tp1_hit_at,:opened_at,
                :closed_at,:close_price,:outcome,:pnl_r,:journaled)""",
            row,
        )
    log.info("trade opened: %s %s @ %.4f (sl %.4f tp1 %.4f tp2 %.4f)",
             candidate["symbol"], direction, fill, sl, candidate["tp1"], candidate["tp2"])
    return row


# ---------- trade resolution (ported "let winners run" from v1) -------------

def _trading_days_between(start: datetime, end: datetime) -> int:
    if end < start:
        return 0
    cur, d1, days = start.date(), end.date(), 0
    while cur < d1:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def _pnl_r(entry: float, original_sl: float, close: float, direction: str) -> float:
    risk = abs(entry - original_sl)
    if risk <= 0:
        return 0.0
    return (close - entry) / risk if direction == "long" else (entry - close) / risk


def resolve_open_trades(prices: dict[str, float]) -> list[dict[str, Any]]:
    """Adjudicate open trades against the latest price snapshot.

    Two-phase "let winners run": pre-TP1 a stop is a LOSS and TP2 a win; once
    TP1 prints we trail the stop to entry and keep running, so a later reversal
    is a 0R BREAKEVEN rather than a loss. Conservative ordering (check stop
    before TP2) since a single snapshot can't reveal intra-bar sequence.
    """
    now = datetime.now(timezone.utc)
    closed: list[dict[str, Any]] = []
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM trades WHERE outcome IS NULL")]
        for t in rows:
            price = prices.get(t["symbol"])
            outcome, close_price = _decide(t, price, now)
            if outcome is None:
                # may have mutated to trailing — persist tp1_hit / trailed stop
                if t.get("_dirty"):
                    c.execute(
                        "UPDATE trades SET tp1_hit=?, tp1_hit_at=?, stop_loss=? WHERE id=?",
                        (t["tp1_hit"], t["tp1_hit_at"], t["stop_loss"], t["id"]),
                    )
                continue
            pnl = round(_pnl_r(t["entry_price"], t["original_sl"], close_price, t["direction"]), 3)
            c.execute(
                "UPDATE trades SET outcome=?, close_price=?, closed_at=?, pnl_r=? WHERE id=?",
                (outcome, close_price, now.isoformat(), pnl, t["id"]),
            )
            t.update(outcome=outcome, close_price=close_price, closed_at=now.isoformat(), pnl_r=pnl)
            closed.append(t)
            log.info("trade closed: %s %s -> %s @ %.4f (%.2fR)",
                     t["symbol"], t["direction"], outcome, close_price, pnl)
    return closed


def _decide(t: dict[str, Any], price: float | None, now: datetime) -> tuple[str | None, float]:
    """Return (outcome, close_price). outcome None means stays open. Mutates
    `t` in place (sets `_dirty`) on the TP1->trailing transition."""
    direction, sl, tp1, tp2 = t["direction"], t["stop_loss"], t["tp1"], t["tp2"]
    tp1_hit = bool(t["tp1_hit"])
    if price is not None:
        if direction == "long":
            if price <= sl:
                return (OUTCOME_BREAKEVEN if tp1_hit else OUTCOME_LOSS,
                        t["entry_price"] if tp1_hit else sl)
            if price >= tp2:
                return OUTCOME_WIN_TP2, tp2
            if not tp1_hit and price >= tp1:
                t.update(tp1_hit=1, tp1_hit_at=now.isoformat(), stop_loss=t["entry_price"], _dirty=True)
                return None, 0.0
        else:
            if price >= sl:
                return (OUTCOME_BREAKEVEN if tp1_hit else OUTCOME_LOSS,
                        t["entry_price"] if tp1_hit else sl)
            if price <= tp2:
                return OUTCOME_WIN_TP2, tp2
            if not tp1_hit and price <= tp1:
                t.update(tp1_hit=1, tp1_hit_at=now.isoformat(), stop_loss=t["entry_price"], _dirty=True)
                return None, 0.0
    try:
        opened = datetime.fromisoformat(str(t["opened_at"]))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
    except ValueError:
        return None, 0.0
    if _trading_days_between(opened, now) >= cfg.EXPIRY_TRADING_DAYS:
        return OUTCOME_EXPIRED, float(price if price is not None else t["entry_price"])
    return None, 0.0


# ---------- reads -----------------------------------------------------------

def list_open_trades() -> list[dict[str, Any]]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM trades WHERE outcome IS NULL ORDER BY opened_at")]


def recent_closed(symbol: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    q = "SELECT * FROM trades WHERE outcome IS NOT NULL"
    args: list[Any] = []
    if symbol:
        q += " AND symbol=?"
        args.append(symbol)
    q += " ORDER BY closed_at DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def unjournaled_closed() -> list[dict[str, Any]]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM trades WHERE outcome IS NOT NULL AND journaled=0 ORDER BY closed_at")]


def mark_journaled(trade_id: str, journal_file: str | None = None) -> None:
    with _conn() as c:
        c.execute("UPDATE trades SET journaled=1 WHERE id=?", (trade_id,))
        if journal_file:
            log.debug("journaled %s -> %s", trade_id, journal_file)


def add_lesson(scope: str, summary: str, journal_file: str | None = None) -> str:
    lid = str(uuid.uuid4())
    with _conn() as c:
        c.execute(
            "INSERT INTO lessons (id, created_ts, scope, summary, journal_file) VALUES (?,?,?,?,?)",
            (lid, _now(), scope, summary, journal_file),
        )
    return lid


def recent_lessons(scope: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    q = "SELECT * FROM lessons"
    args: list[Any] = []
    if scope:
        q += " WHERE scope=? OR scope='global'"
        args.append(scope)
    q += " ORDER BY created_ts DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def recent_signal_for_dedup(symbol: str, direction: str, since: datetime) -> list[dict[str, Any]]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM signals WHERE symbol=? AND direction=? AND scan_ts>=? ORDER BY scan_ts DESC",
            (symbol, direction, since.isoformat()))]


# ---------- stats -----------------------------------------------------------

def symbol_stats(symbol: str) -> dict[str, Any]:
    closed = recent_closed(symbol, limit=10_000)
    wins = sum(1 for t in closed if t["outcome"] in _WIN)
    losses = sum(1 for t in closed if t["outcome"] in _LOSS)
    decided = wins + losses
    rs = [t["pnl_r"] for t in closed if t["pnl_r"] is not None]
    return {
        "symbol": symbol,
        "n_closed": len(closed),
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / decided) if decided else None,
        "avg_r": (sum(rs) / len(rs)) if rs else None,
        "total_r": round(sum(rs), 3) if rs else 0.0,
        "meaningful": decided >= 5,
    }


def system_stats() -> dict[str, Any]:
    closed = recent_closed(limit=10_000)
    wins = sum(1 for t in closed if t["outcome"] in _WIN)
    losses = sum(1 for t in closed if t["outcome"] in _LOSS)
    decided = wins + losses
    rs = [t["pnl_r"] for t in closed if t["pnl_r"] is not None]
    return {
        "total_closed": len(closed),
        "open": len(list_open_trades()),
        "win_rate": (wins / decided) if decided else None,
        "total_r": round(sum(rs), 3) if rs else 0.0,
    }
