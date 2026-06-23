"""OPTIONAL LLM adapter — OFF by default.

The bot runs entirely free on the deterministic brain (brain.py). This module
is only imported when LLM_ENABLED is true (BOT_LLM=1 and the selected
provider has a key). It adds nuanced judgment + richer journal prose.

Providers
---------
groq       (default, FREE) — Llama 3.3 70B via Groq's OpenAI-compatible API.
           Fast, runs from GitHub Actions, no cost. Get a key at
           https://console.groq.com and set GROQ_API_KEY.
anthropic  (paid)          — Claude. Opt in with BOT_LLM_PROVIDER=anthropic.

Both go through `_complete_json`, so judge()/reflect() are provider-agnostic.
Any failure (network, bad JSON) returns None and brain.py falls back to the
deterministic path — an LLM hiccup can never break a scan.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from v2 import config as cfg

log = logging.getLogger(__name__)

_JUDGE_SYSTEM = """You are a disciplined trading risk officer reviewing one SMC \
(Smart Money Concepts) candidate. You are given the mechanical setup and the \
bot's own memory of how similar past trades resolved. Be skeptical: most \
setups should be skipped. Weigh the symbol's real track record above the \
pattern's elegance. Respond ONLY with a JSON object and nothing else:
{"take": true|false, "confidence": "low"|"medium"|"high",
 "size": "none"|"quarter"|"half"|"full", "rationale": "<=2 sentences"}"""

_REFLECT_SYSTEM = """You are a trading coach writing a short post-mortem of one \
resolved trade so the bot can learn. Be concrete and honest about what the \
outcome implies. Respond ONLY with a JSON object and nothing else:
{"story": "2-3 sentences on what happened",
 "lesson": "1-2 sentences of transferable guidance"}"""

_CANDIDATE_FIELDS = ("symbol", "direction", "setups", "score", "regime", "price",
                     "entry_low", "entry_high", "stop_loss", "tp1", "tp2", "rr")
_TRADE_FIELDS = ("symbol", "direction", "setups", "regime", "entry_price", "original_sl",
                 "tp1", "tp2", "tp1_hit", "outcome", "pnl_r", "opened_at", "closed_at")


# ---------- provider dispatch ----------------------------------------------

def _complete_json(system: str, user: str, *, max_tokens: int) -> dict[str, Any] | None:
    if cfg.LLM_PROVIDER == "anthropic":
        text = _anthropic_complete(system, user, max_tokens)
    else:
        text = _groq_complete(system, user, max_tokens)
    if not text:
        return None
    return _parse_json(text)


def _groq_complete(system: str, user: str, max_tokens: int) -> str | None:
    """Groq's OpenAI-compatible chat completions, JSON mode on. JSON mode needs
    the literal word 'json' in the prompt — the system prompts already have it."""
    try:
        resp = httpx.post(
            f"{cfg.GROQ_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.GROQ_API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": cfg.GROQ_MODEL,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "max_tokens": max_tokens,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError) as e:
        log.warning("Groq call failed: %s", e)
        return None


def _anthropic_complete(system: str, user: str, max_tokens: int) -> str | None:
    """Claude via the Messages API. Lazily imports the SDK so the dep is only
    needed when this provider is actually selected."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
        model = cfg.JUDGE_MODEL  # both calls use the judge model for simplicity
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(getattr(b, "text", "") for b in resp.content)
    except Exception as e:  # SDK/network/auth — fall back deterministically
        log.warning("Anthropic call failed: %s", e)
        return None


def _parse_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("LLM returned non-JSON: %s", text[:200])
        return None


# ---------- public API ------------------------------------------------------

def _subset(d: dict[str, Any], fields: tuple[str, ...]) -> str:
    return json.dumps({k: d.get(k) for k in fields}, indent=2, default=str)


def judge(candidate: dict[str, Any], memory_block: str) -> dict[str, Any] | None:
    user = f"Candidate:\n{_subset(candidate, _CANDIDATE_FIELDS)}\n\nMemory:\n{memory_block}\n"
    out = _complete_json(_JUDGE_SYSTEM, user, max_tokens=400)
    if not out or "take" not in out:
        return None
    out["source"] = f"{cfg.LLM_PROVIDER}:{cfg.GROQ_MODEL if cfg.LLM_PROVIDER == 'groq' else cfg.JUDGE_MODEL}"
    return out


def reflect(trade: dict[str, Any], memory_block: str) -> dict[str, Any] | None:
    user = f"Resolved trade:\n{_subset(trade, _TRADE_FIELDS)}\n\nContext from memory:\n{memory_block}\n"
    out = _complete_json(_REFLECT_SYSTEM, user, max_tokens=400)
    if not out or "story" not in out:
        return None
    return out
