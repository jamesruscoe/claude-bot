"""FX email alerts — FORMATTING + trigger decision only. NO SMTP here.

The actual send is done by the existing `dawidd6/action-send-mail` step in
scan-fx.yml (the same Gmail action the equities workflow uses) — this module only
decides *whether* to alert and writes a subject line + a phone-readable body to
two files in the workspace root, which the workflow reads and mails. Keeping the
send in the workflow means there is exactly one email system in the repo, and
keeping this module send-free makes the formatting and trigger logic unit-testable
without a network or credentials.

Two triggers, both FX-only:
  1. SIGNAL — a paper trade was OPENED (i.e. a candidate cleared FX_MIN_SCORE,
     was taken, and passed the session/news/correlation open-gates). Alerting on
     OPENS (not raw candidates) keeps the email set identical to the paper ledger:
     you are only ever told about trades the bot actually recorded, so your
     evidence base never drifts from what you'd have done. The pipeline's dedup
     means a symbol you're already in won't re-alert.
  2. FEED HEALTH — guards against a silent stale feed (the frozen-cache episode).
     Immediate email if a scan detected NO setups because every pair was rejected
     for a feed reason; and a backstop email after FX_HEALTH_ZERO_RUNS consecutive
     zero-DETECTION scans (keyed on detections, not opens — see config).

Everything here is best-effort: callers wrap it, and it also guards internally,
so an alert failure can never crash a scan or lose a recorded paper trade.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from v2 import config as cfg

log = logging.getLogger(__name__)


def _sym(symbol: str) -> str:
    """'GBPUSD=X' -> 'GBPUSD' for human-facing text."""
    return symbol.replace("=X", "").replace("=", "")


def _fmt_px(x: Any) -> str:
    try:
        return f"{float(x):.5f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(x)


# --------------------------------------------------------------------------- #
# Signal alert (opened paper trades)                                           #
# --------------------------------------------------------------------------- #

def _signal_block(row: dict[str, Any]) -> str:
    sym = _sym(row["symbol"])
    direction = str(row.get("direction") or "").upper()
    score = row.get("score")
    rr = row.get("rr")
    size = row.get("size")
    return "\n".join([
        f"{sym}  {direction}",
        f"score {score} · R:R {rr} · size {size}",
        "",
        f"entry  {_fmt_px(row.get('entry'))}",
        f"stop   {_fmt_px(row.get('stop_loss'))}",
        f"TP1    {_fmt_px(row.get('tp1'))}",
        f"TP2    {_fmt_px(row.get('tp2'))}",
    ])


def build_signal_alert(opened_rows: list[dict[str, Any]]) -> tuple[str, str] | None:
    """(subject, body) for one or more opened trades, or None if none."""
    if not opened_rows:
        return None
    first = opened_rows[0]
    subj = (f"FX SIGNAL: {_sym(first['symbol'])} "
            f"{str(first.get('direction') or '').upper()} @ {_fmt_px(first.get('entry'))}")
    if len(opened_rows) > 1:
        subj += f" (+{len(opened_rows) - 1} more)"
    body = "FX SIGNAL\n\n" + "\n\n---\n\n".join(_signal_block(r) for r in opened_rows)
    body += ("\n\nPaper trade — recorded in the FX ledger (state-fx branch). "
             "No live order was placed.")
    return subj, body


# --------------------------------------------------------------------------- #
# Feed-health state + alert                                                    #
# --------------------------------------------------------------------------- #

def _read_health() -> dict[str, Any]:
    try:
        return json.loads(cfg.ALERT_HEALTH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"consecutive_zero": 0, "alert_active": False}


def _write_health(state: dict[str, Any]) -> None:
    try:
        cfg.ensure_state_dirs()
        cfg.ALERT_HEALTH_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError as e:
        log.debug("alert health write failed: %s", e)


def _health_alert_text(*, feed_dead: bool, streak: int) -> tuple[str, str]:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if feed_dead:
        subj = "FX ALERT: feed looks dead (all pairs rejected)"
        why = ("This scan detected NO setups because EVERY pair was rejected for a "
               "feed reason (stale / no data). That is the signature of a frozen or "
               "failed data feed — the same failure mode as the silent stale-cache "
               "episode.")
    else:
        subj = f"FX ALERT: {streak} scans with zero setups detected"
        why = (f"The detectors have found NO setups across the whole basket for "
               f"{streak} consecutive scans (>= the {cfg.FX_HEALTH_ZERO_RUNS} threshold). "
               "The feed is returning data, but a frozen-but-fresh-looking feed would "
               "look exactly like this — worth a check. It may also just be a quiet "
               "market.")
    body = "\n".join([
        "FX FEED HEALTH ALERT", "", why, "",
        f"Consecutive zero-detection scans: {streak}",
        f"Time (UTC): {ts}", "",
        "The paper bot did NOT trade. Check the yfinance feed / the state-fx cache.",
        "No further health emails will be sent until a scan detects setups again.",
    ])
    return subj, body


def _handle_health(*, feed_dead: bool, detections: int) -> tuple[str, str] | None:
    """Update the consecutive-zero streak and return an alert (subject, body) if
    one should fire this scan, else None. Edge-triggered: fires once when the
    problem starts, then stays quiet until a detection clears it."""
    state = _read_health()
    if detections > 0:
        _write_health({"consecutive_zero": 0, "alert_active": False})
        return None
    streak = int(state.get("consecutive_zero", 0)) + 1
    active = bool(state.get("alert_active", False))
    should = feed_dead or streak >= cfg.FX_HEALTH_ZERO_RUNS
    alert = None
    if should and not active:
        alert = _health_alert_text(feed_dead=feed_dead, streak=streak)
        active = True
    _write_health({"consecutive_zero": streak, "alert_active": active})
    return alert


# --------------------------------------------------------------------------- #
# File output (consumed by the workflow's mail step)                           #
# --------------------------------------------------------------------------- #

def write_alert_files(subject: str, body: str) -> None:
    try:
        cfg.ALERT_SUBJECT_FILE.write_text(subject.strip() + "\n", encoding="utf-8")
        cfg.ALERT_BODY_FILE.write_text(body, encoding="utf-8")
        log.info("FX alert queued: %s", subject)
    except OSError as e:
        log.warning("could not write FX alert files (non-fatal): %s", e)


def write_test_alert() -> None:
    """Manual delivery check (--email-test / workflow_dispatch test_email)."""
    body = ("FX alerting test.\n\n"
            "If you're reading this, delivery works. If it landed in spam, mark it "
            "'not spam' so real signals reach the inbox.\n\n"
            "Real alerts look like:  FX SIGNAL: GBPUSD LONG @ 1.2840\n\n"
            "Triggered manually — no scan was run and no trade was recorded.")
    write_alert_files("FX SIGNAL TEST — delivery check", body)


# --------------------------------------------------------------------------- #
# Entry point (called from pipeline._emit, FX-gated + fail-open)               #
# --------------------------------------------------------------------------- #

def evaluate_and_write(payload: dict[str, Any]) -> None:
    """Decide whether to alert from a scan payload and write the files. FX only;
    never raises (best-effort)."""
    try:
        if not cfg.FX_ENABLED:
            return

        # Skipped scans: only a "no data" abort is a feed problem; a weekend/
        # market-closed skip is not (and must not touch the streak).
        if payload.get("skipped"):
            if "no data" in str(payload.get("reason", "")).lower():
                alert = _handle_health(feed_dead=True, detections=0)
                if alert:
                    write_alert_files(*alert)
            return

        results = payload.get("results") or []
        opened_rows = [r for r in results if r.get("opened")]
        if opened_rows:
            sig = build_signal_alert(opened_rows)
            if sig:
                write_alert_files(*sig)
            # An open implies detections > 0 — clear any health streak.
            _write_health({"consecutive_zero": 0, "alert_active": False})
            return

        detections = sum(1 for r in results if r.get("candidate"))
        # Whole feed dead: the scan ran but produced no rows at all (every pair
        # was stale-skipped, which appends no row) and opened nothing.
        feed_dead = (detections == 0 and not results and not payload.get("opened"))
        alert = _handle_health(feed_dead=feed_dead, detections=detections)
        if alert:
            write_alert_files(*alert)
    except Exception as e:  # noqa: BLE001 — alerts must never break a scan
        log.warning("FX alert evaluation failed (non-fatal): %s", e)
