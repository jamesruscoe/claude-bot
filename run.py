"""claude-bot v2 entrypoint.

    python run.py                 # one live scan (gated on market open)
    python run.py --force         # scan even if the market is closed (manual)
    python run.py --selftest      # offline end-to-end proof, no network/API
    python run.py --resolve-only  # just adjudicate open trades against fresh data

The deterministic, memory-driven brain runs for free. Claude judgment is an
opt-in upgrade (set ANTHROPIC_API_KEY and BOT_LLM=1) — leave it off until the
free version has proven an edge. See ARCHITECTURE.md.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from v2.config import LOG_FILE, ensure_state_dirs


def _setup_logging() -> None:
    ensure_state_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="claude-bot v2 — memory-driven SMC scanner")
    parser.add_argument("--force", action="store_true", help="scan even if the market is closed")
    parser.add_argument("--selftest", action="store_true", help="offline end-to-end self-test")
    parser.add_argument("--resolve-only", action="store_true",
                        help="only resolve open trades, don't look for new ones")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    _setup_logging()
    from v2 import pipeline

    if args.selftest:
        ok = pipeline.selftest()
        sys.exit(0 if ok else 1)

    asyncio.run(pipeline.run_scan(force=args.force))


if __name__ == "__main__":
    main()
