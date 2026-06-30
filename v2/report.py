"""Daily paper-trading report (Phase 5).

Turns the latest scan payload + the durable ledger into a human-readable daily
summary: candidates and their verdicts, rejections grouped by reason, paper
trades opened/closed this run, and the running (sized) expectancy. Paper only —
fills are simulated against yfinance bars + the assumed spread (see levels.py);
there is no live execution anywhere.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from v2 import config as cfg
from v2 import store

log = logging.getLogger(__name__)

REPORT_FILE = cfg.STATE_DIR / "daily_report.md"


def _load_last_scan() -> dict | None:
    try:
        return json.loads(cfg.SCAN_OUTPUT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build_report(payload: dict | None = None) -> str:
    payload = payload or _load_last_scan()
    st = store.system_stats()
    rej = store.rejection_counts()
    so = store.second_opinion_agreement()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    lines = [f"# Daily report — {now}", ""]
    if payload is None:
        lines.append("_No scan payload found (run `python run.py` first)._")
        return "\n".join(lines)

    if payload.get("skipped"):
        lines.append(f"Scan skipped — {payload.get('reason')}.")
    else:
        lines.append(f"Market: **{payload.get('market', '?')}** · "
                     f"judge: {'Claude' if payload.get('llm') else 'deterministic'} · paper only")
        cands = [r for r in payload.get("results", []) if r.get("candidate")]
        lines.append(f"\n## Candidates ({len(cands)})")
        if not cands:
            lines.append("- none today")
        for r in sorted(cands, key=lambda r: (r.get("take", False), r.get("score", 0)), reverse=True):
            verdict = "TAKE" if r.get("take") else "skip"
            op = " ← opened" if r.get("opened") else ""
            blk = f" · blocked: {r['fx_blocked']}" if r.get("fx_blocked") else ""
            lines.append(f"- **{r['symbol']}** {r.get('direction','—')} score {r.get('score')} "
                         f"R:R {r.get('rr')} — {verdict}/{r.get('confidence')}/{r.get('size')}{op}{blk}")

        opened, closed = payload.get("opened", []), payload.get("closed", [])
        if opened:
            lines.append(f"\n## Opened this run ({len(opened)})")
            for t in opened:
                lines.append(f"- {t['symbol']} {t['direction']} @ {t['entry_price']} "
                             f"(sl {t['stop_loss']} tp1 {t['tp1']} tp2 {t['tp2']}, size {t.get('size')})")
        if closed:
            lines.append(f"\n## Resolved this run ({len(closed)})")
            for t in closed:
                lines.append(f"- {t['symbol']} {t['outcome']} — {t['pnl_r']}R (sized)")

    wr = f"{st['win_rate']*100:.0f}%" if st["win_rate"] is not None else "n/a"
    lines.append("\n## Ledger (running, sized R)")
    lines.append(f"- {st['open']} open · {st['total_closed']} closed · win rate {wr} "
                 f"· total {st['total_r']:+.2f}R")
    if so["agreement_rate"] is not None:
        lines.append(f"- Claude↔deterministic second-opinion agreement: "
                     f"{so['agreement_rate']*100:.0f}% over {so['n']}")

    if rej:
        lines.append("\n## Rejections by reason (all-time)")
        for reason, n in sorted(rej.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {reason}: {n}")

    lines.append("\n> Paper only. Live bid/ask via an OANDA practice account is a documented "
                 "TODO (see ARCHITECTURE.md / PROGRESS.md) — fills here use yfinance mid + assumed spread.")
    return "\n".join(lines)


def write_daily_report(payload: dict | None = None) -> str:
    text = build_report(payload)
    try:
        cfg.ensure_state_dirs()
        REPORT_FILE.write_text(text, encoding="utf-8")
        log.info("wrote %s", REPORT_FILE)
    except OSError as e:
        log.debug("report write failed: %s", e)
    return text
