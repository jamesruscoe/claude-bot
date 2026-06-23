"""claude-bot v2 — a memory-driven SMC trading research bot.

The v2 rebuild separates three concerns the v1 scanner collapsed into one pass:

  1. SIGNAL ENGINE (deterministic)  — salvaged pure-math SMC detectors propose
     candidates + levels. They no longer make the final call.
  2. JUDGMENT (Claude)              — each candidate is weighed against retrieved
     memories of how similar past trades resolved, producing a structured
     take/skip decision with a written rationale.
  3. MEMORY (durable)              — a SQLite ledger (hard source of truth) plus a
     markdown journal (narrative lessons Claude reads to think). Both persist
     across runs via the `state` git branch, so the bot never forgets.

See ARCHITECTURE.md for the full picture.
"""
