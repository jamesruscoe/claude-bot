"""OPTIONAL Claude adapter — OFF by default.

This is the dormant upgrade path. The bot runs entirely free on the
deterministic brain (brain.py); this module is only imported when LLM_ENABLED
is true (ANTHROPIC_API_KEY set *and* BOT_LLM=1). Until you've proven the free
version has an edge, leave it off — every call here costs money.

⚠ Before enabling: validate this against the current Anthropic API reference
   (model ids, message shape, token params). It is written to the stable
   Messages API but has not been run against a live key in this build.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from v2.config import ANTHROPIC_API_KEY, JUDGE_MODEL, REFLECT_MODEL

log = logging.getLogger(__name__)

_JUDGE_SYSTEM = """You are a disciplined trading risk officer reviewing one SMC \
(Smart Money Concepts) candidate. You are given the mechanical setup and the \
bot's own memory of how similar past trades resolved. Be skeptical: most \
setups should be skipped. Weigh the symbol's real track record above the \
pattern's elegance. Respond ONLY with a JSON object:
{"take": bool, "confidence": "low"|"medium"|"high",
 "size": "none"|"quarter"|"half"|"full", "rationale": "<=2 sentences"}"""

_REFLECT_SYSTEM = """You are a trading coach writing a short post-mortem of one \
resolved trade so the bot can learn. Be concrete and honest about what the \
outcome implies. Respond ONLY with a JSON object:
{"story": "2-3 sentences on what happened",
 "lesson": "1-2 sentences of transferable guidance"}"""


def _client():
    import anthropic  # imported lazily so the dep is only needed when enabled
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _ask_json(model: str, system: str, user: str, max_tokens: int) -> dict[str, Any] | None:
    resp = _client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(getattr(b, "text", "") for b in resp.content).strip()
    # tolerate a fenced block
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("LLM returned non-JSON: %s", text[:200])
        return None


_CANDIDATE_FIELDS = ("symbol", "direction", "setups", "score", "regime", "price",
                     "entry_low", "entry_high", "stop_loss", "tp1", "tp2", "rr")
_TRADE_FIELDS = ("symbol", "direction", "setups", "regime", "entry_price", "original_sl",
                 "tp1", "tp2", "tp1_hit", "outcome", "pnl_r", "opened_at", "closed_at")


def _subset(d: dict[str, Any], fields: tuple[str, ...]) -> str:
    return json.dumps({k: d.get(k) for k in fields}, indent=2, default=str)


def judge(candidate: dict[str, Any], memory_block: str) -> dict[str, Any] | None:
    user = f"Candidate:\n{_subset(candidate, _CANDIDATE_FIELDS)}\n\nMemory:\n{memory_block}\n"
    out = _ask_json(JUDGE_MODEL, _JUDGE_SYSTEM, user, max_tokens=400)
    if not out:
        return None
    out["source"] = f"claude:{JUDGE_MODEL}"
    return out


def reflect(trade: dict[str, Any], memory_block: str) -> dict[str, Any] | None:
    user = f"Resolved trade:\n{_subset(trade, _TRADE_FIELDS)}\n\nContext from memory:\n{memory_block}\n"
    return _ask_json(REFLECT_MODEL, _REFLECT_SYSTEM, user, max_tokens=400)
