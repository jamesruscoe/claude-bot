"""v2 configuration.

Everything stateful lives under STATE_DIR so the GitHub Actions workflow can
point it at a checked-out `state` branch and commit it back at the end of the
run. Nothing here writes outside STATE_DIR except logs.
"""
from __future__ import annotations

import os
from pathlib import Path

# Reuse the v1 universe + ticker mappings — they're just data, no behaviour.
from config import (  # noqa: F401  (re-exported for convenience)
    MASSIVE_API_KEY,
    OB_IMPULSE_OVERRIDES,
    TRADINGVIEW_SYMBOLS,
    WATCHLIST,
    YAHOO_TICKERS,
    YFINANCE_DAILY_SYMBOLS,
)

ROOT_DIR = Path(__file__).resolve().parent.parent

# --- Durable state location -------------------------------------------------
# In CI this is set to the path of the checked-out `state` branch. Locally it
# defaults to ./state next to the repo so a dev run mirrors production layout.
STATE_DIR = Path(os.getenv("BOT_STATE_DIR", str(ROOT_DIR / "state")))
DB_PATH = STATE_DIR / "ledger.db"
JOURNAL_DIR = STATE_DIR / "journal"          # one markdown file per resolved trade
LESSONS_DIR = STATE_DIR / "lessons"          # distilled, cross-trade lessons
SCAN_OUTPUT_FILE = STATE_DIR / "last_scan.json"  # latest brief, for the dashboard/email

# --- LLM (OPTIONAL, OFF BY DEFAULT) -----------------------------------------
# The system runs entirely free on the deterministic, memory-driven brain
# (see brain.py). Claude judgment is a dormant upgrade you switch on AFTER the
# free version has proven it has an edge — so you don't pay an API bill to find
# out whether the strategy works. Enable by setting ANTHROPIC_API_KEY *and*
# BOT_LLM=1. The llm.py adapter must be validated against the current Anthropic
# API reference before you flip this on.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
JUDGE_MODEL = os.getenv("BOT_JUDGE_MODEL", "claude-sonnet-4-6")
REFLECT_MODEL = os.getenv("BOT_REFLECT_MODEL", "claude-opus-4-8")
LLM_ENABLED = bool(ANTHROPIC_API_KEY) and os.getenv("BOT_LLM", "0") == "1"

# --- Signal gating ----------------------------------------------------------
# The deterministic engine still scores 0/50/100. We only hand candidates to
# the judge at or above this score — below it there isn't enough structure to
# reason about. The judge is the thing that decides take vs skip; this is just
# a noise floor.
CANDIDATE_MIN_SCORE = 50

# --- Levels -----------------------------------------------------------------
# v1's biggest accuracy bug was greedy, liquidity-derived targets sitting
# 2.5-3.5 ATR away regardless of risk — they almost never filled, so trades
# bled to the stop or expired. v2 uses a TIGHT structure-based stop (just
# beyond the zone) and sets targets as R-MULTIPLES of that risk. A tight stop
# keeps 1R small, so TP1 at 2R is close enough to actually print, and R:R is
# correct by construction.
SL_ATR_MULT = 0.5      # stop sits 0.5 ATR beyond the zone edge — tight
TP1_R_MULT = 2.0       # first target at 2R; banks + trails stop to breakeven
TP2_R_MULT = 3.0       # runner at 3R
MAX_RISK_PCT = 0.08    # reject setups whose stop is > 8% of price (too wide/illiquid)
SL_BUFFER_PCT = 0.005  # fallback when ATR is unavailable

# --- Trade lifecycle --------------------------------------------------------
EXPIRY_TRADING_DAYS = 10   # force-close + tag EXPIRED after this many weekdays
MEMORY_RETRIEVAL_K = 6     # how many past trades/lessons to show the judge

# --- Signal dedup -----------------------------------------------------------
DEDUP_WINDOW_HOURS = 18    # suppress an identical fresh signal within this window
DEDUP_ZONE_PCT = 0.005     # "identical" = same dir + zone edges within 0.5%

# --- Market calendar --------------------------------------------------------
MARKET_CALENDAR = "XNYS"   # NYSE; pandas-market-calendars handles holidays
# A scan is "stale" if the freshest daily bar is older than this many calendar
# days — guards against firing on a feed that hasn't updated.
MAX_BAR_STALENESS_DAYS = 4

LOG_FILE = STATE_DIR / "bot.log"


def ensure_state_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    LESSONS_DIR.mkdir(parents=True, exist_ok=True)
