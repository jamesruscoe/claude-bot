import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --- API keys ---
# Massive (formerly Polygon). The same key works on api.polygon.io and api.massive.com.
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY", "")

# --- Paths ---
ROOT_DIR = Path(__file__).parent
TRADES_FILE = ROOT_DIR / "trades.json"
SCAN_RESULTS_FILE = ROOT_DIR / "scan_results.json"
WATCHING_STATE_FILE = ROOT_DIR / "watching_state.json"
BACKTEST_RESULTS_FILE = ROOT_DIR / "backtest_results.json"
COOLING_OFF_FILE = ROOT_DIR / "cooling_off.json"
LOG_FILE = ROOT_DIR / "trading_bot.log"
STATIC_DIR = ROOT_DIR / "static"

# --- Scan universe (broad, dynamic) ---
# 20 liquid US stocks + commodity/index proxies. Every scan covers all 20;
# the daily briefing surfaces only the top 3 by confluence score. There is
# no static curation any more — symbols that work will be revealed by the
# rolling win rate computed from logged outcomes (memory.compute_win_rate).
#
# symbol → Polygon/Massive ticker (used for daily candles + reference news).
# Aliases: USOIL→USO, XAUUSD→GLD, NQ1→QQQ.
WATCHLIST: dict[str, str] = {
    "NVDA":   "NVDA",
    "AAPL":   "AAPL",
    "TSLA":   "TSLA",
    "MSFT":   "MSFT",
    "META":   "META",
    "AMZN":   "AMZN",
    "AMD":    "AMD",
    "GOOGL":  "GOOGL",
    "NFLX":   "NFLX",
    "CRM":    "CRM",
    "UBER":   "UBER",
    "COIN":   "COIN",
    "PLTR":   "PLTR",
    "APLD":   "APLD",
    "ARM":    "ARM",
    "SMCI":   "SMCI",
    "MSTR":   "MSTR",
    "USOIL":  "USO",
    "XAUUSD": "GLD",
    "NQ1":    "QQQ",
}

# Yahoo Finance tickers (yfinance) for intraday data + news + live price.
# Most symbols are direct equities so Yahoo and Polygon use the same ticker;
# the three commodity/index aliases use a sympathetic proxy on each side
# (CL=F for crude futures, GLD for gold ETF, QQQ for Nasdaq-100).
YAHOO_TICKERS: dict[str, str] = {
    "NVDA":   "NVDA",
    "AAPL":   "AAPL",
    "TSLA":   "TSLA",
    "MSFT":   "MSFT",
    "META":   "META",
    "AMZN":   "AMZN",
    "AMD":    "AMD",
    "GOOGL":  "GOOGL",
    "NFLX":   "NFLX",
    "CRM":    "CRM",
    "UBER":   "UBER",
    "COIN":   "COIN",
    "PLTR":   "PLTR",
    "APLD":   "APLD",
    "ARM":    "ARM",
    "SMCI":   "SMCI",
    "MSTR":   "MSTR",
    "USOIL":  "CL=F",
    "XAUUSD": "GLD",
    "NQ1":    "QQQ",
}

# Lookback window for 1H bars from Yahoo. 730 days is yfinance's hard cap on
# the 1H interval — request the maximum so the intraday detector has the
# longest possible runway. Equities (RTH-only) yield ~5000 bars at this
# window; CL=F (near-24h) yields ~16000.
INTRADAY_LOOKBACK_DAYS = 730

# Per-symbol Order Block impulse-threshold overrides. The default in
# smc_detector.OB_IMPULSE_THRESHOLD (3%) is calibrated for large caps;
# smaller caps move in smaller increments so a 3% bar over 1–3 days is
# rarer — the lower threshold gives the detector more candidates.
OB_IMPULSE_OVERRIDES: dict[str, float] = {
    "APLD": 0.02,
}

# Per-symbol historical win rate at the 10-day TP1-vs-SL horizon. Updated
# after each backtest run. Used by the daily briefing to display a coarse
# probability estimate per signal.
# Per-symbol historical win rates are no longer hardcoded. The brief reads a
# rolling rate from `memory.compute_win_rate(symbol)` derived from the live
# trade log (trades.json). Symbols start with no track record and build one
# as outcomes are logged via the dashboard.

# Assets that are sensitive to ongoing geopolitical conflict — always warn.
GEOPOLITICAL_ASSETS = {"USOIL", "XAUUSD"}

# Trading windows by asset class (GMT). Anything not listed defaults to the
# US session window, which is correct for the equity majority of the universe.
TRADING_WINDOWS: dict[str, str] = {
    "USOIL":  "13:30-15:30 GMT (US session + EIA Wednesdays)",
    "XAUUSD": "08:00-16:00 GMT (London + US overlap)",
}

# --- API base ---
MASSIVE_BASE_URL = "https://api.polygon.io"  # api.massive.com also works
MASSIVE_RATE_LIMIT_PER_MIN = 5  # free tier
MASSIVE_RETRY_MAX = 3

# --- SMC scoring thresholds ---
# The two-setup detector emits 0, 50, or 100. Threshold 75 means only
# dual-confluence (both OB retest AND BOS retest, same direction) fires.
SCAN_MIN_SCORE = 50      # below this, don't even mention in scan summary
DISPLAY_MIN_SCORE = 60   # daily-briefing top-3 cutoff — under this, "no quality setups today"
ANALYSIS_MIN_SCORE = 80  # below this, take_trade is forced false (raised 75→80 in choppy 2026 regime)

# --- Stop-loss placement ---
# SL = OB extreme ± max(SL_BUFFER_PCT × price,  SL_ATR_MULT × ATR14)
#
# ATR multiplier intentionally set to 0 after the 2026-05-08 ATR-fix
# experiment dropped aggregate 10-day win rate from 52.2% → 18.8%. The
# tight 0.3% buffer pairs with tight TP1 which actually fills within
# the 10-day horizon on these volatile names. Re-enabling ATR scaling
# would require also changing the TP1 multiplier (currently 2× risk).
SL_BUFFER_PCT = 0.003   # 0.3%
SL_ATR_MULT = 0.0       # disabled — see comment above

# --- Signal dedup window (bars) ---
# After a take-trade signal fires, identical signals (same direction +
# entry zone + SL) within this many bars are suppressed.
SIGNAL_DEDUP_BARS = 5

# --- Dynamic cooling-off blacklist ---
# When a symbol's rolling 30-day track record falls below COOLDOWN_WR_THRESHOLD
# on at least COOLDOWN_MIN_RESOLVED resolved trades, it auto-enters a
# cooldown for COOLDOWN_DAYS days. State lives in COOLING_OFF_FILE.
COOLDOWN_DAYS = 30
COOLDOWN_MIN_RESOLVED = 3
COOLDOWN_WR_THRESHOLD = 0.30  # below this is "broken setup territory"

# One-shot bootstrap. Symbols listed here are marked cooling-off the first
# time the cooling_off module imports IF they're not already present in the
# state file. Driven by the 2026-only backtest:
#   MSTR  0/3 (resolved)
#   COIN  0/2
#   PLTR  0/1 (1 still open — counted as 0/1 here)
#   GOOGL 0/1
#   CRM   0/1
#   XAUUSD 0/1
INITIAL_COOLDOWN_SEED: dict[str, dict[str, int | str]] = {
    "MSTR":   {"wins": 0, "losses": 3, "reason": "0/3 in 2026 backtest"},
    "COIN":   {"wins": 0, "losses": 2, "reason": "0/2 in 2026 backtest"},
    "PLTR":   {"wins": 0, "losses": 1, "reason": "0/1 in 2026 backtest"},
    "GOOGL":  {"wins": 0, "losses": 1, "reason": "0/1 in 2026 backtest"},
    "CRM":    {"wins": 0, "losses": 1, "reason": "0/1 in 2026 backtest"},
    "XAUUSD": {"wins": 0, "losses": 1, "reason": "0/1 in 2026 backtest"},
}

# --- Watch loop interval ---
# Massive free tier returns EOD daily candles only, so re-checking faster than
# once a day mostly burns API calls — but a small interval is still useful for
# picking up the daily bar shortly after market close.
WATCH_INTERVAL_SECONDS = 4 * 60 * 60  # 4 hours

# --- Dashboard ---
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8000
DASHBOARD_URL = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"

# --- HTF bias window (in daily bars) ---
HTF_BIAS_BARS = 60  # last 60 days of daily candles

# --- Backtest config ---
# 120 daily bars (~6 months trading) of warmup gives the SMA-50 regime
# filter, the 60-bar HTF bias, and the OB/BOS detectors enough history
# before the walk-forward starts firing signals.
BACKTEST_WARMUP_BARS = 120
BACKTEST_HORIZONS = (5, 10)  # forward-look windows in trading days

# Cut-off date for backtest signal *firing* — bars before this date are still
# used as detector context (warmup + swings + OBs) but no signals fire from
# them. 2025 was unrepresentative of current regime; we want a read on what's
# working NOW. Format: ISO date.
BACKTEST_FROM_DATE = "2026-01-01"

# --- Timeframe configs (multiplier, timespan, lookback_bars) ---
# Daily ONLY. Massive free tier doesn't return usable intraday data
# (1H comes back empty/sparse, 4H is always resultsCount=0). `bars` is the
# *retain* count — we over-fetch from the API and trim to the most recent
# `bars` here. 2000 covers ~8 years of trading days, well beyond what the
# Polygon free tier returns in practice.
TIMEFRAMES = {
    "D": {"multiplier": 1, "timespan": "day", "bars": 2000},
}


def assert_configured() -> None:
    if not MASSIVE_API_KEY:
        raise RuntimeError(
            "Missing MASSIVE_API_KEY (or POLYGON_API_KEY). "
            "Copy .env.example to .env and fill in the value."
        )
