"""Markdown journal + memory retrieval.

The SQLite ledger is the structured truth; the journal is the *narrative* — one
markdown file per resolved trade and per distilled lesson, written by the
reflection layer. It's human-readable (browsable straight on the `state`
branch on GitHub) and it's what the judge reads to "think" about a new
candidate in light of what actually happened before.

Frontmatter mirrors the memory pattern: a slug, a one-line description used
for relevance, and metadata. The body holds the story + the lesson.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from v2 import config as cfg
from v2 import store

log = logging.getLogger(__name__)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def write_trade_entry(trade: dict[str, Any], *, story: str, lesson: str) -> Path:
    """Write a journal markdown file for a resolved trade. `story` and `lesson`
    are produced by the reflection layer (Claude, or a templated fallback)."""
    cfg.ensure_state_dirs()
    closed = (trade.get("closed_at") or datetime.now(timezone.utc).isoformat())[:10]
    name = f"{closed}-{_slug(trade['symbol'] + '-' + trade['direction'])}-{trade['id'][:8]}.md"
    path = cfg.JOURNAL_DIR / name
    setups = trade.get("setups")
    if isinstance(setups, str):
        try:
            setups = json.loads(setups)
        except json.JSONDecodeError:
            setups = [setups]
    desc = (f"{trade['symbol']} {trade['direction']} via "
            f"{', '.join(setups or ['?'])} -> {trade['outcome']} ({trade.get('pnl_r')}R)")
    body = f"""---
name: {_slug(name[:-3])}
description: {desc}
metadata:
  type: trade
  symbol: {trade['symbol']}
  direction: {trade['direction']}
  setups: {json.dumps(setups or [])}
  regime: {trade.get('regime')}
  outcome: {trade['outcome']}
  pnl_r: {trade.get('pnl_r')}
  opened_at: {trade.get('opened_at')}
  closed_at: {trade.get('closed_at')}
---

# {trade['symbol']} {trade['direction'].upper()} — {trade['outcome']} ({trade.get('pnl_r')}R)

Entry {trade['entry_price']} · stop {trade['original_sl']} · TP1 {trade['tp1']} · TP2 {trade['tp2']}

## What happened
{story}

## Lesson
{lesson}
"""
    path.write_text(body, encoding="utf-8")
    log.info("journal entry written: %s", path.name)
    return path


def write_lesson(scope: str, summary: str, body: str) -> Path:
    """Write a standalone, cross-trade lesson the judge can retrieve by scope."""
    cfg.ensure_state_dirs()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = cfg.LESSONS_DIR / f"{stamp}-{_slug(scope)}.md"
    path.write_text(
        f"""---
name: lesson-{_slug(scope)}-{stamp}
description: {summary}
metadata:
  type: lesson
  scope: {scope}
---

{body}
""",
        encoding="utf-8",
    )
    store.add_lesson(scope, summary, journal_file=path.name)
    log.info("lesson written: %s", path.name)
    return path


def _read_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    # strip frontmatter for the prose the LLM reads
    m = re.match(r"^---\n.*?\n---\n(.*)$", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def retrieve_for(candidate: dict[str, Any], *, k: int = cfg.MEMORY_RETRIEVAL_K) -> dict[str, Any]:
    """Assemble the memory context for a candidate: the symbol's track record,
    the most relevant recent resolved trades, and applicable lessons.

    Relevance is intentionally simple (symbol + setup overlap + same direction
    weighting) — the judge does the nuanced reasoning; this just puts the right
    history in front of it.
    """
    symbol = candidate["symbol"]
    cand_setups = set(candidate.get("setups") or [])
    cand_dir = candidate.get("direction")

    closed = store.recent_closed(limit=200)

    def relevance(t: dict[str, Any]) -> tuple:
        t_setups = set(json.loads(t["setups"]) if t.get("setups") else [])
        return (
            t["symbol"] == symbol,
            len(cand_setups & t_setups),
            t["direction"] == cand_dir,
            t.get("closed_at") or "",
        )

    ranked = sorted(closed, key=relevance, reverse=True)[:k]

    # Pull journal prose for the ranked trades when it exists.
    journal_files = {p.stem: p for p in cfg.JOURNAL_DIR.glob("*.md")} if cfg.JOURNAL_DIR.exists() else {}
    memories: list[dict[str, Any]] = []
    for t in ranked:
        entry = {
            "symbol": t["symbol"], "direction": t["direction"],
            "setups": json.loads(t["setups"]) if t.get("setups") else [],
            "regime": t.get("regime"), "outcome": t["outcome"],
            "pnl_r": t.get("pnl_r"), "opened_at": t.get("opened_at"),
            "prose": None,
        }
        for stem, path in journal_files.items():
            if t["id"][:8] in stem:
                entry["prose"] = _read_body(path)
                break
        memories.append(entry)

    lessons = []
    for scope in (symbol, *(cand_setups or []), "global"):
        for ls in store.recent_lessons(scope=scope, limit=3):
            lessons.append({"scope": ls["scope"], "summary": ls["summary"]})
    # dedupe lessons by summary
    seen, deduped = set(), []
    for ls in lessons:
        if ls["summary"] in seen:
            continue
        seen.add(ls["summary"])
        deduped.append(ls)

    return {
        "symbol_stats": store.symbol_stats(symbol),
        "memories": memories,
        "lessons": deduped[:k],
    }


def render_memory_block(retrieval: dict[str, Any]) -> str:
    """Human/LLM-readable text rendering of retrieved memory."""
    s = retrieval["symbol_stats"]
    lines = [
        f"Track record for {s['symbol']}: {s['n_closed']} closed, "
        f"win rate {('%.0f%%' % (s['win_rate'] * 100)) if s['win_rate'] is not None else 'n/a'}, "
        f"avg {('%.2fR' % s['avg_r']) if s['avg_r'] is not None else 'n/a'} "
        f"({'meaningful sample' if s['meaningful'] else 'thin sample — treat as weak prior'}).",
    ]
    if retrieval["memories"]:
        lines.append("\nMost similar past trades:")
        for m in retrieval["memories"]:
            head = (f"- {m['symbol']} {m['direction']} [{', '.join(m['setups'])}] "
                    f"regime={m['regime']} -> {m['outcome']} ({m['pnl_r']}R)")
            lines.append(head)
            if m.get("prose"):
                # one-line the lesson section if present
                lines.append(f"    note: {m['prose'][:280].replace(chr(10), ' ')}")
    if retrieval["lessons"]:
        lines.append("\nApplicable lessons:")
        for ls in retrieval["lessons"]:
            lines.append(f"- ({ls['scope']}) {ls['summary']}")
    return "\n".join(lines)
