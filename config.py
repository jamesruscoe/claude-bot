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
LOG_FILE = ROOT_DIR / "trading_bot.log"
STATIC_DIR = ROOT_DIR / "static"

# --- Watchlist (final curation by backtest) ---
# symbol → Massive/Polygon ticker. USOIL uses USO ETF as a proxy.
#
# Only names with demonstrated edge on score-100 dual-confluence signals
# (10-day TP1-vs-SL win rate). SPY/MSFT/QQQ/AAPL all removed for failing
# the backtest. HISTORICAL_WIN_RATES_10D below is filled in from the most
# recent backtest run after the ATR+dedup fixes.
WATCHLIST: dict[str, str] = {
    "USOIL": "USO",
    "NVDA":  "NVDA",
    "TSLA":  "TSLA",
    "APLD":  "APLD",
}

# Yahoo Finance tickers (yfinance) for intraday data. Massive's free tier doesn't
# return usable 1H/4H/15M aggregates, but yfinance does — we fetch 1H from
# Yahoo and synthesise 4H locally so the detector can also analyse intraday
# structure. CL=F is the front-month NYMEX crude futures contract, which
# tracks USOIL / WTI tightly (closer than the USO ETF proxy). APLD trades
# directly on Nasdaq so no proxy is needed.
YAHOO_TICKERS: dict[str, str] = {
    "USOIL": "CL=F",
    "NVDA":  "NVDA",
    "TSLA":  "TSLA",
    "APLD":  "APLD",
}

# Lookback window for 1H bars from Yahoo. 730 days is yfinance's hard cap on
# the 1H interval — request the maximum so the intraday detector has the
# longest possible runway. Equities (RTH-only) yield ~5000 bars at this
# window; CL=F (near-24h) yields ~16000.
INTRADAY_LOOKBACK_DAYS = 730

# Symbols on the live watchlist that have NOT yet passed a backtest. Their
# briefs get a paper-only warning AND `take_trade` is forced to False, so no
# live alert fires — they're still scanned for data collection only.
#
# - APLD: only 6 backtest signals, statistically inconclusive. Holds here
#   until ≥20 signals accumulated.
# - TSLA: 10-day win rate dropped to 45.5% on 11 signals (below the 50%
#   breakeven required for 1:2 R:R) on the 2026-05-08 backtest. Demoted
#   to paper-only pending another data refresh.
UNVALIDATED_SYMBOLS: set[str] = {"APLD", "TSLA"}

# Per-symbol Order Block impulse-threshold overrides. The default in
# smc_detector.OB_IMPULSE_THRESHOLD (3%) is calibrated for large caps like
# NVDA/TSLA; smaller caps (APLD) move in smaller increments so a 3% bar over
# 1-3 days is rarer — the lower threshold gives the detector more candidates.
OB_IMPULSE_OVERRIDES: dict[str, float] = {
    "APLD": 0.02,
}

# Per-symbol historical win rate at the 10-day TP1-vs-SL horizon. Updated
# after each backtest run. Used by the daily briefing to display a coarse
# probability estimate per signal.
# Active live-trading symbols only. TSLA + APLD are scanned but unvalidated
# (see UNVALIDATED_SYMBOLS) and therefore intentionally absent here.
HISTORICAL_WIN_RATES_10D: dict[str, float] = {
    "NVDA":  0.750,
    "USOIL": 0.667,
}

# Assets that are sensitive to ongoing geopolitical conflict — always warn.
GEOPOLITICAL_ASSETS = {"USOIL", "XAUUSD"}

# Trading windows by asset class (GMT). Controls the "best_window" suggestion.
TRADING_WINDOWS: dict[str, str] = {
    "USOIL": "13:30-15:30 GMT (US session + EIA Wednesdays)",
    "NVDA":  "13:30-16:00 GMT (US open + first 2.5h)",
    "TSLA":  "13:30-16:00 GMT (US open + first 2.5h)",
    "APLD":  "13:30-16:00 GMT (US open + first 2.5h)",
}

# --- API base ---
MASSIVE_BASE_URL = "https://api.polygon.io"  # api.massive.com also works
MASSIVE_RATE_LIMIT_PER_MIN = 5  # free tier
MASSIVE_RETRY_MAX = 3

# --- SMC scoring thresholds ---
# The two-setup detector emits 0, 50, or 100. Threshold 75 means only
# dual-confluence (both OB retest AND BOS retest, same direction) fires.
SCAN_MIN_SCORE = 50      # below this, don't even mention in daily briefing
ANALYSIS_MIN_SCORE = 75  # below this, take_trade is forced false

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
