"""Offline batch second-opinion (Phase 4).

A nightly job that re-judges the day's candidates with Claude via the **Message
Batches API** (50% of standard price) and records the verdicts alongside the
live deterministic decisions. This is purely OBSERVATIONAL — it never changes a
live decision; it exists so Claude's judgment can be compared against the free
deterministic judge over time before any decision to switch the live judge on.

Only candidates the detector actually produced are sent — never per-bar, never
the whole universe. Any failure is logged and skipped; it can't affect trading.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from v2 import config as cfg
from v2 import journal, llm, store

log = logging.getLogger(__name__)


def _candidate_from_signal(sig: dict) -> dict:
    return {
        "symbol": sig["symbol"], "score": sig["score"], "direction": sig["direction"],
        "setups": json.loads(sig["setups"]) if sig.get("setups") else [],
        "regime": sig.get("regime"), "price": sig.get("price"), "atr": sig.get("atr"),
        "entry_low": sig.get("entry_low"), "entry_high": sig.get("entry_high"),
        "stop_loss": sig.get("stop_loss"), "tp1": sig.get("tp1"), "tp2": sig.get("tp2"),
        "rr": sig.get("rr"),
    }


def run_batch_second_opinion(*, lookback_hours: int = 24) -> dict:
    """Re-judge the last `lookback_hours` of candidates via the Batch API."""
    store.init_db()
    if cfg.LLM_PROVIDER != "anthropic" or not cfg.ANTHROPIC_API_KEY:
        log.warning("batch second-opinion needs BOT_LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY")
        return {"skipped": True, "reason": "no anthropic key"}

    since = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    signals = store.signals_since(since)
    if not signals:
        log.info("batch second-opinion: no candidates in the last %dh", lookback_hours)
        return {"skipped": True, "reason": "no candidates", "n": 0}

    try:
        import anthropic
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request
    except ImportError:
        log.warning("anthropic SDK not installed — `pip install anthropic`")
        return {"skipped": True, "reason": "no sdk"}

    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    by_id: dict[str, dict] = {}
    requests = []
    for sig in signals:
        cand = _candidate_from_signal(sig)
        memory = journal.render_memory_block(journal.retrieve_for(cand))
        by_id[sig["id"]] = sig
        requests.append(Request(
            custom_id=sig["id"],
            params=MessageCreateParamsNonStreaming(
                model=cfg.JUDGE_MODEL, max_tokens=400, system=llm._JUDGE_SYSTEM,
                messages=[{"role": "user", "content": llm._judge_user(cand, memory)}],
            ),
        ))

    batch = client.messages.batches.create(requests=requests)
    log.info("batch %s submitted — %d candidates; polling…", batch.id, len(requests))

    import time
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        time.sleep(30)

    recorded = 0
    for result in client.messages.batches.results(batch.id):
        sig = by_id.get(result.custom_id)
        if sig is None or result.result.type != "succeeded":
            continue
        msg = result.result.message
        u = getattr(msg, "usage", None)
        if u is not None:
            llm.log_cost(cfg.JUDGE_MODEL, getattr(u, "input_tokens", 0),
                         getattr(u, "output_tokens", 0), batch=True)
        text = "".join(getattr(b, "text", "") for b in msg.content)
        verdict = llm.parse_verdict(llm._parse_json(text))
        if not verdict:
            continue
        live = store.live_decision_for(sig["id"])
        agrees = None if live is None else (bool(live["take"]) == bool(verdict["take"]))
        store.record_second_opinion(sig["id"], verdict, agrees=agrees)
        recorded += 1

    stats = store.second_opinion_agreement()
    log.info("batch second-opinion: recorded %d verdict(s); agreement so far %s",
             recorded, f"{stats['agreement_rate']*100:.0f}%" if stats["agreement_rate"] is not None else "n/a")
    print(f"\nBatch second-opinion — {recorded} judged. "
          f"Claude↔deterministic agreement: "
          f"{stats['agreement_rate']*100:.0f}% over {stats['n']}" if stats["agreement_rate"] is not None
          else f"\nBatch second-opinion — {recorded} judged.")
    return {"recorded": recorded, "agreement": stats}
