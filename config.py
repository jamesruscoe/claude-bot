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
}

# Yahoo Finance tickers (yfinance) for intraday data. Massive's free tier doesn't
# return usable 1H/4H/15M aggregates, but yfinance does — we fetch 1H from
# Yahoo and synthesise 4H locally so the detector can also analyse intraday
# structure. CL=F is the front-month NYMEX crude futures contract, which
# tracks USOIL / WTI tightly (closer than the USO ETF proxy).
YAHOO_TICKERS: dict[str, str] = {
    "USOIL": "CL=F",
    "NVDA":  "NVDA",
    "TSLA":  "TSLA",
}

# Lookback window for 1H bars from Yahoo. 30 days × ~7 RTH bars/day on equities
# ≈ 210 bars; CL=F is near-24h so 30d ≈ 600+ bars. Plenty for the SMC detector.
INTRADAY_LOOKBACK_DAYS = 30

# No unvalidated symbols in the live watchlist any more.
UNVALIDATED_SYMBOLS: set[str] = set()

# Per-symbol historical win rate at the 10-day TP1-vs-SL horizon. Updated
# after each backtest run. Used by the daily briefing to display a coarse
# probability estimate per signal.
HISTORICAL_WIN_RATES_10D: dict[str, float] = {
    "USOIL": 1.000,
    "NVDA":  0.667,
    "TSLA":  0.625,
}

# Assets that are sensitive to ongoing geopolitical conflict — always warn.
GEOPOLITICAL_ASSETS = {"USOIL", "XAUUSD"}

# Trading windows by asset class (GMT). Controls the "best_window" suggestion.
TRADING_WINDOWS: dict[str, str] = {
    "USOIL": "13:30-15:30 GMT (US session + EIA Wednesdays)",
    "NVDA":  "13:30-16:00 GMT (US open + first 2.5h)",
    "TSLA":  "13:30-16:00 GMT (US open + first 2.5h)",
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
BACKTEST_WARMUP_BARS = 60  # need this much history before generating signals
BACKTEST_HORIZONS = (5, 10)  # forward-look windows in trading days

# --- Timeframe configs (multiplier, timespan, lookback_bars) ---
# Daily ONLY. Massive free tier doesn't return usable intraday data
# (1H comes back empty/sparse, 4H is always resultsCount=0).
TIMEFRAMES = {
    "D": {"multiplier": 1, "timespan": "day", "bars": 250},
}


def assert_configured() -> None:
    if not MASSIVE_API_KEY:
        raise RuntimeError(
            "Missing MASSIVE_API_KEY (or POLYGON_API_KEY). "
            "Copy .env.example to .env and fill in the value."
        )
