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
# (see brain.py). An LLM judge/reflector is a dormant upgrade you switch on
# with BOT_LLM=1 once you want richer reasoning + journal prose.
#
# Default provider is Groq's FREE hosted API (Llama 3.3 70B) — fast, works from
# GitHub Actions, no cost. Anthropic (Claude) is also supported but paid, so
# it's opt-in via BOT_LLM_PROVIDER=anthropic. Either way nothing fires unless
# BOT_LLM=1 and the selected provider has a key.
LLM_PROVIDER = os.getenv("BOT_LLM_PROVIDER", "groq")  # "groq" | "anthropic"

# Groq (free tier) — get a key at https://console.groq.com
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_BASE_URL = os.getenv("BOT_GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL = os.getenv("BOT_GROQ_MODEL", "llama-3.3-70b-versatile")

# Anthropic (paid) — only if you deliberately switch provider.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Haiku 4.5 is the candidate judge (Phase 4) — only candidates reach it, never
# per-bar/per-universe, so a small fast model is the right tool + cheapest.
JUDGE_MODEL = os.getenv("BOT_JUDGE_MODEL", "claude-haiku-4-5")
REFLECT_MODEL = os.getenv("BOT_REFLECT_MODEL", "claude-opus-4-8")

# Per-MTok pricing for the cost log (USD). Source: claude-api skill, 2026-06.
MODEL_PRICING = {
    "claude-haiku-4-5":  {"in": 1.00, "out": 5.00},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00},
    "claude-opus-4-8":   {"in": 5.00, "out": 25.00},
}
COST_LOG_FILE = STATE_DIR / "llm_cost.jsonl"
# Batch API (offline nightly second-opinion) runs at 50% of standard price.
BATCH_DISCOUNT = 0.5


def _provider_key_present() -> bool:
    return bool(GROQ_API_KEY) if LLM_PROVIDER == "groq" else bool(ANTHROPIC_API_KEY)


LLM_ENABLED = _provider_key_present() and os.getenv("BOT_LLM", "0") == "1"

# --- Market mode (NEW — defaults to equities so the existing path is unchanged)
# "equities" keeps the original Massive/SMC behaviour bit-for-bit. "fx" routes
# the whole pipeline through the yfinance FX adapter + pip/spread risk math.
# "fx_oanda" swaps that feed for the OANDA v20 practice adapter (real bid/ask
# candles) while keeping every downstream strategy/risk path identical.
# Switch with BOT_MARKET=fx (or fx_oanda). Everything FX-specific is gated on
# FX_ENABLED, which is true for BOTH fx feeds so the pip/spread math routes the
# same way; FX_OANDA additionally selects the real-quote source.
MARKET = os.getenv("BOT_MARKET", "equities").lower()
FX_ENABLED = MARKET in ("fx", "fx_oanda")
FX_OANDA = MARKET == "fx_oanda"

# FX basket — yfinance tickers. Majors + the two EUR crosses the brief names.
FX_BASKET = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "AUDUSD=X",
    "USDCAD=X", "NZDUSD=X", "EURGBP=X", "EURJPY=X",
]

# Pip size per pair. JPY-quoted pairs move in 0.01; everything else 0.0001.
def fx_pip_size(symbol: str) -> float:
    return 0.01 if "JPY=X" in symbol else 0.0001

# Assumed CONSERVATIVE spread in pips. Yahoo serves mid prices, so we never see
# a real bid/ask — we assume a fixed, deliberately-wide spread per pair and bake
# it into entry + R:R. JPY crosses are wider. (OANDA practice can replace these
# with real quotes later — see Phase 5 TODO.)
FX_SPREAD_PIPS: dict[str, float] = {
    "EURUSD=X": 0.6, "GBPUSD=X": 1.0, "USDCHF=X": 1.2, "AUDUSD=X": 1.0,
    "USDCAD=X": 1.2, "NZDUSD=X": 1.4, "EURGBP=X": 1.2, "USDJPY=X": 1.0,
    "EURJPY=X": 1.8,
}
FX_DEFAULT_SPREAD_PIPS = 1.5  # fallback for any pair not listed

def fx_spread_pips(symbol: str) -> float:
    return FX_SPREAD_PIPS.get(symbol, FX_DEFAULT_SPREAD_PIPS)

# Fixed-fractional risk per trade and nominal paper account, for lot sizing.
# Risk in R is independent of lots; lots are recorded for realism only.
FX_RISK_PCT = float(os.getenv("BOT_FX_RISK_PCT", "0.005"))   # 0.5% of equity / trade
FX_ACCOUNT_EQUITY = float(os.getenv("BOT_FX_EQUITY", "10000"))  # paper account, USD
FX_STD_LOT_UNITS = 100_000   # 1.0 lot = 100k base units

# yfinance is unofficial + delayed: cache pulls and never act on empty/old data.
FX_CACHE_TTL_SECONDS = int(os.getenv("BOT_FX_CACHE_TTL", "900"))  # 15 min
CACHE_DIR = STATE_DIR / "cache"

# --- Range-breakout pattern — PRE-REGISTERED parameters (PATTERN_RANGE_BREAKOUT.md)
# These are chosen A PRIORI with reasoning and FROZEN before any TRAIN measurement,
# so "calibrate on TRAIN" cannot quietly become a sweep across their product. All
# ATR-scaled (scale-free across pairs). Env overrides exist for later phases, but
# the pre-registered defaults are the ones the Gate-1 screen is run at. Do not tune.
FX_RANGE_LOOKBACK = int(os.getenv("BOT_FX_RANGE_LOOKBACK", "40"))       # bars to form the range (~8wk daily)
FX_RANGE_MIN_TOUCHES = int(os.getenv("BOT_FX_RANGE_MIN_TOUCHES", "2"))  # swing touches per boundary (2 = definitional min)
FX_RANGE_EQ_ATR = float(os.getenv("BOT_FX_RANGE_EQ_ATR", "0.5"))        # "same level" cluster tol (also enforces flatness)
FX_RANGE_MAX_ATR = float(os.getenv("BOT_FX_RANGE_MAX_ATR", "4.0"))      # max range width R-S (contained consolidation)
FX_RANGE_BRK_ATR = float(os.getenv("BOT_FX_RANGE_BRK_ATR", "0.25"))     # close beyond boundary to confirm a breakout

# --- Multi-pattern detector (PATTERN_SCOPE.md; P0 = accounting only) ---------
# Per-pattern enable registry. Existing SMC (ob/bos) default ON — behaviour is
# unchanged. Every NEW pattern defaults OFF (safe/off standing rule); they are
# implemented + TRAIN-calibrated one at a time in later phases. Each is
# overridable by env: BOT_FX_PATTERN_<UPPER>=0/1.
_FX_PATTERN_DEFAULTS = {
    "ob_retest": True, "bos_retest": True,      # existing SMC — unchanged
    "double_top": False, "double_bottom": False,
    "hns": False, "inv_hns": False,
    "triangle_asc": False, "triangle_desc": False, "triangle_sym": False,
    "range_breakout": False,
}
FX_PATTERNS = {
    name: os.getenv(f"BOT_FX_PATTERN_{name.upper()}", "1" if on else "0") == "1"
    for name, on in _FX_PATTERN_DEFAULTS.items()
}

def fx_pattern_enabled(pattern: str) -> bool:
    return FX_PATTERNS.get(pattern, False)

# Portfolio exposure caps for the wider signal stream (LOCKED, PATTERN_SCOPE §3.5).
# Caps only ever BLOCK, so both are safe-by-default and inert while only OB/BOS runs.
FX_MAX_OPEN = int(os.getenv("BOT_FX_MAX_OPEN", "5"))              # total concurrent open trades
FX_MAX_PER_PATTERN = int(os.getenv("BOT_FX_MAX_PER_PATTERN", "2"))  # concurrent open per pattern

# Per-pattern confidence (PATTERN_SCOPE §3.4). Confidence is FORWARD-ONLY and
# derived from that pattern's own measured expectancy + sample size — never shape.
# Below N_CONF_MIN resolved FORWARD trades a pattern is 'unproven'. NOTE: at n=30
# the SE of mean R is ~0.31R, so 'provisional' is barely more than unproven, not
# validation — real validation is n>=150 forward. Real money: unproven => size 0.
FX_CONF_MIN_N = int(os.getenv("BOT_FX_CONF_MIN_N", "30"))    # unproven -> provisional
FX_CONF_PROVEN_N = int(os.getenv("BOT_FX_CONF_PROVEN_N", "150"))  # provisional -> proven (== registered n)

# --- OANDA v20 practice adapter (real bid/ask candles; DATA ONLY) -----------
# See OANDA_ADAPTER_SCOPE.md. Practice environment only, Bearer-token auth, and
# ONLY the candles/pricing data endpoints are ever touched — no orders, trades,
# or positions endpoint is imported or wired anywhere. Secrets come from env and
# are never committed. The account id is not needed for the candles endpoint
# (kept only for optional current-pricing calls); do not scope a live token.
from datetime import datetime, timezone  # noqa: E402  (local to the OANDA block)

OANDA_API_TOKEN = os.getenv("OANDA_API_TOKEN", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_HOST = os.getenv("BOT_OANDA_HOST", "api-fxpractice.oanda.com")  # PRACTICE ONLY
OANDA_MAX_CANDLES = 5000          # v20 hard cap per request; paginate via from/to
OANDA_CANDLE_START = os.getenv("BOT_OANDA_START", "2004-01-01")  # earliest pull attempt

# Yahoo-style basket symbol -> OANDA v20 instrument name.
OANDA_INSTRUMENTS = {
    "EURUSD=X": "EUR_USD", "GBPUSD=X": "GBP_USD", "USDJPY=X": "USD_JPY",
    "USDCHF=X": "USD_CHF", "AUDUSD=X": "AUD_USD", "USDCAD=X": "USD_CAD",
    "NZDUSD=X": "NZD_USD", "EURGBP=X": "EUR_GBP", "EURJPY=X": "EUR_JPY",
}

# --- FX email alerting (reuses the existing dawidd6 mail ACTION in the workflow;
#     Python only FORMATS the alert + decides when to fire — it never sends) ----
# The scan writes a subject line + a phone-readable body to these files in the
# workspace root (NOT under STATE_DIR, so they are never persisted to the state
# branch). scan-fx.yml reads them and fires the same Gmail SMTP action the
# equities workflow uses. Absent files => no email.
ALERT_SUBJECT_FILE = ROOT_DIR / "fx_alert_subject.txt"
ALERT_BODY_FILE = ROOT_DIR / "fx_alert_body.txt"
# Feed-health backstop state (consecutive zero-DETECTION scans). Lives in
# STATE_DIR so the streak survives across runs on the state-fx branch.
ALERT_HEALTH_FILE = STATE_DIR / "alert_health.json"
# Fire the "feed looks suspiciously quiet" alert after this many CONSECUTIVE
# scans that detected zero setups. Keyed on DETECTIONS (any OB/BOS candidate),
# NOT opened trades: opens are rare (~18/yr) so a zero-OPEN streak is normal,
# but a zero-DETECTION streak across the whole 9-pair basket is the signature of
# a frozen/stale feed (the silent stale-cache episode ran 17 such scans). Default
# 8 ≈ 1.5 trading weeks — well before 17, but tolerant of a genuinely quiet run.
# A truly dead feed (every pair rejected for a feed reason) alerts IMMEDIATELY,
# separately from this counter. Raise if the live ledger shows quiet streaks.
FX_HEALTH_ZERO_RUNS = int(os.getenv("BOT_FX_HEALTH_ZERO_RUNS", "8"))

# --- Out-of-sample discipline (LOCKED — chosen trade-blind, pre-data) --------
# TRAIN / HOLDOUT split boundary. Fixed as a calendar date BEFORE any OANDA data
# was pulled, so it cannot have been nudged to flatter a trade count (that would
# be the p-hacking the pre-registration exists to prevent — see scope §1).
# TRAIN  = daily bars strictly before this date (older ~70%, tuning is allowed).
# HOLDOUT = bars on/after it (most recent ~30%, evaluated ONCE in Phase C, never
# peeked at during Phase A/B). Do NOT move this after seeing any result.
TRAIN_HOLDOUT_BOUNDARY = "2021-01-01"

# Trailing daily bars the detectors see per decision in the OANDA replay. The
# LIVE bot fetches period="3y" (~780 daily bars), so on OANDA's 15-20yr history
# the walk MUST cap the detector window to the same span — otherwise the
# detectors would run on inputs the deployed strategy never sees (a fidelity
# bug), and the walk would be O(n^2). This is faithfulness to production, NOT a
# tuned parameter. ~3 trading years to match FXSource period="3y".
FX_LIVE_DAILY_LOOKBACK = 780

# Registered acceptance criterion — LOCKED 2026-07-21, does NOT move after Gate 2:
#   mean R NET of measured bid/ask, on RESOLVED DUAL-CONFLUENCE trades (score==100),
#   n >= 150, one-sided 95% bootstrap CI lower bound > 0  (bar ~ +0.23R).
# Revising the metric, n, or bar after a result is seen is reject-on-sight.
FX_REGISTERED_MIN_N = 150


def train_holdout_boundary() -> datetime:
    """The split boundary as a tz-aware (UTC) datetime for comparing to Bar.dt."""
    y, m, d = (int(x) for x in TRAIN_HOLDOUT_BOUNDARY.split("-"))
    return datetime(y, m, d, tzinfo=timezone.utc)

# --- FX strategy filters (Phase 2; all FX-only, gated on FX_ENABLED) --------
# Detector calibration: FX daily ranges are <1%, so the equities 3% OB impulse
# almost never fires. Calibrate the impulse threshold for FX. (Phase 3 tunes the
# score threshold against measured expectancy — this just makes OB fireable.)
FX_OB_IMPULSE_THRESHOLD = float(os.getenv("BOT_FX_OB_IMPULSE", "0.008"))  # 0.8%

# Session filter (UTC hours). "off" = no restriction (safe default, behaviour
# unchanged). "overlap" = London/NY overlap only. "skip_asia" = block thin Asia
# hours for non-JPY pairs. Mainly bites once Phase 5 runs intraday.
FX_SESSION_MODE = os.getenv("BOT_FX_SESSION", "off")  # off | overlap | skip_asia
FX_OVERLAP_UTC = (12, 16)     # London/NY overlap, [start, end)
FX_ASIA_UTC = (23, 8)         # Tokyo session wraps midnight

# Correlation-aware exposure cap (audit gap). One macro view must not open as six
# tickets. Cap the number of open trades pushing the SAME currency the SAME way.
# A cap only ever blocks, so it's the conservative direction — on by default.
FX_MAX_PER_CCY = int(os.getenv("BOT_FX_MAX_PER_CCY", "2"))

# Scheduled high-impact news avoidance (ForexFactory weekly JSON). Block opening
# a trade within +/- this many minutes of a high-impact event for either of the
# pair's currencies. Fail-OPEN: if the feed is unreachable we log and don't block
# (so a feed outage can't silently freeze the bot), but the attempt is recorded.
FX_NEWS_FILTER = os.getenv("BOT_FX_NEWS", "1") == "1"
FX_NEWS_WINDOW_MIN = int(os.getenv("BOT_FX_NEWS_WINDOW_MIN", "45"))
FF_CALENDAR_URL = os.getenv(
    "BOT_FF_CALENDAR_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")

# Regime filter period (kept — audit confirmed it's correct caution). The 50-bar
# default is fine for FX daily too; exposed for re-fitting.
FX_REGIME_MA_PERIOD = int(os.getenv("BOT_FX_REGIME_MA", "50"))

# Minimum detector score to OPEN an FX trade (live gate, distinct from the
# CANDIDATE_MIN_SCORE noise floor used for reasoning/logging).
#
# Set to 85 after the robustness review (see CALIBRATION.md). The detector emits
# DISCRETE scores {0, 50, 100}, so any threshold in (50, 100] selects exactly the
# dual-confluence (score==100) set: +0.35R avg over 89 resolved trades (meaningful,
# 25% WR), and that edge HOLDS at 1.5x the assumed per-pair spread (+0.35R) — the
# test that matters most, since the edge sits on top of assumed mid-price spreads.
# The 85-99 band is empty today (no signal scores there), so 85 is operationally
# identical to 100 unless the detector is later recalibrated to emit intermediate
# scores — at which point 85 would also admit those. score>=50 stays marginal
# (+0.05R, ~breakeven) and is deliberately excluded. STILL PAPER-ONLY — let the
# live ledger confirm before any scale-up or going live.
FX_MIN_SCORE = int(os.getenv("BOT_FX_MIN_SCORE", "85"))

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
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
