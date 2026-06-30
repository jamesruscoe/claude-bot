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
    # httpx logs the full request URL at INFO — and the Massive API key rides in
    # the query string. Silence it so the key never lands in logs or artifacts.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="claude-bot v2 — memory-driven SMC scanner")
    parser.add_argument("--force", action="store_true", help="scan even if the market is closed")
    parser.add_argument("--selftest", action="store_true", help="offline end-to-end self-test")
    parser.add_argument("--resolve-only", action="store_true",
                        help="only resolve open trades, don't look for new ones")
    parser.add_argument("--llm-test", action="store_true",
                        help="send one sample judgment to the configured LLM provider to verify the key")
    parser.add_argument("--replay", action="store_true",
                        help="walk-forward replay over history → BASELINE.md (honest, sized R)")
    parser.add_argument("--calibrate", action="store_true",
                        help="replay + write CALIBRATION.md (frequency vs expectancy, proposed threshold)")
    parser.add_argument("--batch-second-opinion", action="store_true",
                        help="offline nightly Claude batch re-judge of the day's candidates (observational)")
    parser.add_argument("--report", action="store_true",
                        help="print/write the daily paper-trading report from the latest scan + ledger")
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

    if args.llm_test:
        sys.exit(0 if _llm_test() else 1)

    if args.replay or args.calibrate:
        from v2 import replay
        replay.main(calibrate=args.calibrate)
        return

    if args.report:
        from v2 import report
        print(report.write_daily_report())
        return

    if args.batch_second_opinion:
        from v2 import batch_judge
        batch_judge.run_batch_second_opinion()
        return

    if args.resolve_only:
        asyncio.run(pipeline.resolve_only())
        return

    asyncio.run(pipeline.run_scan(force=args.force))


def _llm_test() -> bool:
    """Verify the configured LLM provider responds with a valid judgment."""
    from v2 import config as cfg
    from v2 import llm
    if cfg.LLM_PROVIDER == "groq" and not cfg.GROQ_API_KEY:
        print("GROQ_API_KEY not set — export it (https://console.groq.com) and retry.")
        return False
    candidate = {
        "symbol": "DEMO", "direction": "long", "setups": ["ob_retest", "bos_retest"],
        "score": 100, "regime": "bullish", "price": 100.0, "entry_low": 99.0,
        "entry_high": 100.0, "stop_loss": 98.0, "tp1": 102.0, "tp2": 103.0, "rr": 2.0,
    }
    memory = ("Track record for DEMO: 8 closed, win rate 62%, avg +0.7R "
              "(meaningful sample).\nMost similar past trades:\n"
              "- DEMO long [ob_retest, bos_retest] regime=bullish -> WIN_TP2 (3.0R)")
    print(f"Calling {cfg.LLM_PROVIDER} ({cfg.GROQ_MODEL if cfg.LLM_PROVIDER == 'groq' else cfg.JUDGE_MODEL})…")
    verdict = llm.judge(candidate, memory)
    if not verdict:
        print("No valid response — check the key/model/network. (Scans still work on the free brain.)")
        return False
    print(f"OK — {verdict}")
    return True


if __name__ == "__main__":
    main()
