# Trading Bot

A local SMC (Smart Money Concepts) trading scanner that pulls **daily** candles from **Massive.com** (formerly Polygon.io), runs deterministic pattern detection in pure Python, and produces structured trade briefs with mathematically-derived entry zone, stop loss, take profits, and R:R.

**Why daily only?** Massive's free Stocks Basic tier serves end-of-day daily candles only — intraday aggregates (1H / 4H / 15M) come back empty. After confirming this, the whole pipeline now operates on daily bars (~250 returned per request, more than enough for SMC).

**No AI API in the analysis loop.** All scoring, level placement, reasoning, and the backtest are deterministic. Use Claude Code (this CLI) or your own judgement to interpret results when needed.

Three modes:

- **`py scan.py`** — daily scan over the watchlist; scores each symbol 0–100; writes `scan_results.json`.
- **`py scan.py --backtest`** — walk-forward backtest on the same daily history. **Run this first to confirm the detector is worth trusting.**
- **`py watch.py --symbol SPY`** — re-checks one symbol on a 4-hour cadence, fires an alert when a setup matures.

A dark-themed dashboard at <http://localhost:8000/dashboard> shows live alerts (left) and the latest daily scan (right).

---

## 1. Install

Requires **Python 3.10+**.

```bash
cd "c:\Code\Claude Bot"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

(macOS / Linux: `source .venv/bin/activate`.)

---

## 2. API key

Just one — Massive.com (formerly Polygon.io). Sign up free at <https://polygon.io/dashboard/api-keys>.

```bash
copy .env.example .env
```

```
MASSIVE_API_KEY=...
```

(`POLYGON_API_KEY` is also accepted as a legacy alias.)

> **No Anthropic key required.** The scanner is fully deterministic.

---

## 3. Watchlist

Hardcoded in [config.py](config.py):

| Symbol  | Massive ticker | Notes                  |
| ------- | -------------- | ---------------------- |
| SPY     | SPY            | S&P 500 ETF            |
| NVDA    | NVDA           | Nvidia                 |
| TSLA    | TSLA           | Tesla                  |
| MSFT    | MSFT           | Microsoft              |
| USOIL   | USO            | Crude oil ETF proxy    |

Edit `WATCHLIST` in `config.py` to add or remove names. Make sure the Massive ticker resolves to an equity or ETF.

---

## 4. Run a backtest first

```bash
py scan.py --backtest
```

What it does:

1. Fetches the full daily history for each watchlist symbol (~250 bars).
2. Walks each series forward from day 60 (the warm-up window). At each step:
   - Treats `bars[:i+1]` as everything we knew at the close of day `i`.
   - Runs the SMC detector + scorer on that history.
   - If the resulting brief says `take_trade`, records it as a fired signal.
   - For each forward horizon (5 and 10 trading days):
     - Was the entry zone touched? (`triggered`)
     - If filled, did **TP1** print before the **SL**? (`win` vs `loss`)
     - If neither resolved within the window: `open`.
3. Prints a per-symbol breakdown plus an aggregate row.
4. Saves the full per-trade audit trail to `backtest_results.json`.

Sample output:

```
══════════════════════════════════════════════════════════════════════
  BACKTEST RESULTS — daily SMC, walk-forward
══════════════════════════════════════════════════════════════════════

  SPY
    bars total:      247
    days simulated:  176  (from day 60 onwards)
    signals fired:   14
     5-day:  triggered  10/14   wins   6  losses   3  open   1  win-rate  60.0%  (of triggered)
    10-day:  triggered  12/14   wins   8  losses   3  open   1  win-rate  66.7%  (of triggered)
  ...
══════════════════════════════════════════════════════════════════════
  AGGREGATE
══════════════════════════════════════════════════════════════════════
   5-day:  fired  62  triggered  44 ( 71.0%)  wins  23  losses  17  open   4  win-rate  52.3%
  10-day:  fired  62  triggered  51 ( 82.3%)  wins  31  losses  18  open   2  win-rate  60.8%
══════════════════════════════════════════════════════════════════════
```

**How to read this:**

- **`fired`** — count of times `take_trade=True` triggered during walk-forward.
- **`triggered`** — of those, how many actually saw price reach the entry zone within the horizon. A high un-trigger count means the detector picks zones that are too far off.
- **`win-rate`** — wins divided by triggered. **This is the number that matters.** If it's not at least 50% with R:R 1:2 setups, the strategy is unprofitable in expectation.
- **`open`** — trade filled but neither TP1 nor SL printed within the horizon. Treat as inconclusive.

> Use the backtest to tune. If win-rate is poor: try lowering `min_impulse_pct` for OBs, raising `ANALYSIS_MIN_SCORE`, or relaxing `tolerance_pct` in `find_liquidity` (all in `config.py` / `smc_detector.py`).

---

## 5. Run a live daily scan

```bash
py scan.py
```

It prints one line per symbol, a sorted summary table, and a full trade brief for any symbol scoring ≥ 60. Results are saved to `scan_results.json` and pushed to the dashboard if it's running.

---

## 6. Watch a single symbol

```bash
py watch.py --symbol SPY
```

Re-runs the daily analysis every 4 hours (configurable via `WATCH_INTERVAL_SECONDS`). Daily candles only refresh once per day after market close, so a 4-hour cadence is enough to catch the new bar shortly after it lands.

The same setup won't re-fire — `(direction, entry_zone_low, entry_zone_high, stop_loss)` forms a stable setup ID for dedup.

Stop with **Ctrl+C**.

---

## 7. Dashboard

```bash
py dashboard.py
```

Then open <http://localhost:8000/dashboard>.

- **Header** — connection dot, last scan timestamp, symbols currently being watched.
- **Left** — live alerts. Win/Loss/Stopped buttons log outcomes to `trades.json`.
- **Right** — daily scan table with score, direction, bias, and a short note. Score ≥ 60 highlighted as ★ WATCH.

The dashboard receives SSE updates when `scan.py` finishes, when `watch.py` fires an alert, and when outcomes are logged.

---

## 8. How the SMC detector works

All operating on daily bars. Each detection in [smc_detector.py](smc_detector.py):

| Detection | Rule |
|---|---|
| **Swings** | `lookback=2` (less strict than intraday): swing high if its high beats the 2 bars on both sides. Same for lows. |
| **Trend** | Bullish if last two swing highs are HH and last two swing lows are HL; bearish if LH/LL; else ranging. |
| **HTF bias** | Trend computed on the **last 60 daily bars**. |
| **BOS** | Most recent close above a prior swing high (bullish) or below a prior swing low (bearish). |
| **CHoCH** | In a prior bearish structure, close above the most recent swing high (bullish CHoCH); mirror for bullish prior. |
| **Order blocks** | Last opposite-coloured candle before a 3-bar impulse ≥ 1% (daily threshold; intraday was 0.5%). Marked mitigated if price returned through the zone. |
| **FVGs** | Standard 3-candle gap pattern with a strong middle candle (body > 50% of range). |
| **Liquidity** | Equal highs/lows within 0.2% of each other; untapped most-recent swings. |
| **RSI / divergence** | 14-period Wilder. Divergence checked at the last two same-direction swings. |

**Confluence score (0–100):**

| Component | Points |
|---|---|
| 60-day bias agrees with full-period structure | +30 |
| Order block aligned with direction | +20 |
| Unmitigated FVG aligned with direction | +15 |
| Liquidity pool sitting as a target (not behind us) | +15 |
| RSI divergence aligned with direction | +10 |
| Recent news headlines exist (live mode only) | +10 |

`take_trade=True` requires score ≥ 60 **and** a valid OB/FVG anchor for the entry zone.

---

## 9. Level math

For each take-trade brief:

- **Entry zone** — the OB/FVG range; `entry` is the midpoint.
- **Stop loss** — 0.3% beyond the OB extreme (`SL_BUFFER_PCT` in `config.py`).
- **TP1 / TP2** — drawn from detected liquidity pools above (long) or below (short). If none exist, falls back to symmetric 1:2 / 1:3 R:R.
- **R:R** — `(TP1 − entry) / (entry − SL)` for longs, mirrored for shorts.
- **Best window** — UK-aware GMT trading window per asset class.

---

## 10. Files

```
trading-bot/
├── scan.py              Live scan + --backtest
├── watch.py             Daily-only single-symbol watcher
├── dashboard.py         FastAPI dashboard server (run separately)
├── analyser.py          Deterministic trade-brief builder
├── smc_detector.py      Daily SMC detection (swings, BOS, CHoCH, OBs, FVGs, liquidity, RSI)
├── market_data.py       Massive REST client (rate-limited, retrying)
├── enricher.py          News + macro calendar
├── memory.py            trades.json read/write
├── config.py            Watchlist, thresholds, paths
├── static/dashboard.html  Plain HTML/CSS/JS
├── requirements.txt
├── .env.example
├── scan_results.json    Live scan output
├── backtest_results.json  Walk-forward backtest output
├── trades.json          Live trade log
├── watching_state.json  watch.py heartbeats
└── trading_bot.log      Append-only log
```

---

## 11. Tuning checklist

In `config.py`:

- **`SCAN_MIN_SCORE`** (50) — anything below this isn't mentioned in the daily briefing.
- **`ANALYSIS_MIN_SCORE`** (60) — `take_trade` is forced false below this score.
- **`SL_BUFFER_PCT`** (0.003) — distance beyond the OB extreme for stop placement.
- **`HTF_BIAS_BARS`** (60) — daily-bar window for the HTF bias.
- **`BACKTEST_WARMUP_BARS`** (60) — bars needed before the backtest starts firing signals.
- **`BACKTEST_HORIZONS`** ((5, 10)) — forward-look windows for outcome evaluation.
- **`WATCH_INTERVAL_SECONDS`** (14400 = 4h) — `watch.py` re-check cadence.

In `smc_detector.py`:

- **`SWING_LOOKBACK`** (2) — bars on each side a swing must beat.
- **`find_order_blocks(min_impulse_pct=0.01)`** — minimum 3-bar impulse to qualify the preceding candle.
- **`find_liquidity(tolerance_pct=0.002)`** — equal high/low tolerance.

---

## 12. Free-tier caveats

- **No intraday data.** Confirmed empty for 1H / 4H / 15M on Stocks Basic. The scanner makes one request per symbol (daily) plus one news request per symbol — comfortably inside the 5-req/min cap.
- **15-min delayed data** even on the daily timeframe (irrelevant for end-of-day analysis).
- **2026 FOMC dates are hardcoded.** Update `FOMC_2026_DATES` in `enricher.py` annually.
- **Single-watch only.** `watch.py` runs one symbol per process; start a second terminal for a second symbol.

---

## 13. Cloud deployment via GitHub Actions

The repo ships with a [`.github/workflows/scan.yml`](.github/workflows/scan.yml) workflow that runs `scan.py` four times per weekday on GitHub-hosted runners and emails you on take-trade signals. Free for public repos; for private repos it bills against your 2,000-min/month free Actions quota — each scan run is < 1 min.

### 13.1 Push to GitHub

If you haven't already:

```bash
git init -b main
git add .
git commit -m "Initial commit"
gh repo create <your-username>/claude-bot --private --source=. --remote=origin --push
```

(Or create the repo in the GitHub UI, then `git remote add origin …` and `git push -u origin main`.)

`.env` is excluded by `.gitignore` — your API key never leaves the machine. Verify with `git status` before the first push.

### 13.2 Configure five GitHub Secrets

GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**. Add all five:

| Secret | Value | Source |
|---|---|---|
| `POLYGON_API_KEY` | your Massive/Polygon key | the same value as `MASSIVE_API_KEY` in your local `.env` |
| `WEBHOOK_SECRET` | any random string | currently reserved — pass-through env var for future webhook auth |
| `EMAIL_TO` | the address to notify | e.g. `you@example.com` |
| `GMAIL_USERNAME` | the Gmail account sending the alert | e.g. `youraccount@gmail.com` |
| `GMAIL_PASSWORD` | a Gmail **app password** | not your normal Gmail password — see below |

### 13.3 Get a Gmail app password

Gmail will not accept your real password over SMTP from a third-party service — you need a 16-character app password.

1. Enable 2-factor auth on the Gmail account (required before app passwords are unlocked): <https://myaccount.google.com/security>
2. Go to <https://myaccount.google.com/apppasswords>
3. Pick **Mail** as the app, name it (e.g. `claude-bot`), click **Create**
4. Copy the 16-character code Google shows (no spaces) into the `GMAIL_PASSWORD` secret. Google won't show it again — generate a fresh one if you lose it

### 13.4 Schedule

The workflow's `cron` triggers (UTC, weekdays only):

| Cron        | UTC   | Local (BST / GMT+1) | Why                       |
|-------------|-------|---------------------|---------------------------|
| `0 7  * * 1-5`  | 07:00 | 08:00 BST           | Pre-Europe open           |
| `0 13 * * 1-5`  | 13:00 | 14:00 BST           | 30 min before US open     |
| `0 15 * * 1-5`  | 15:00 | 16:00 BST           | 90 min after US open      |
| `0 20 * * 1-5`  | 20:00 | 21:00 BST           | After US close — fresh EOD bar |

GitHub-hosted cron is best-effort — runs can be 5–15 min late under platform load. Don't rely on second-level precision.

### 13.5 Manual run

The workflow also exposes `workflow_dispatch`. From the **Actions** tab, pick **SMC Scan** → **Run workflow** to trigger it ad hoc — useful for testing the secrets without waiting for the next cron tick.

### 13.6 What you get back

- **Email** to `EMAIL_TO` whenever any symbol scores ≥ `ANALYSIS_MIN_SCORE` and the live-price staleness check doesn't invalidate the setup. Subject: `Trading Bot — Signal fired: NVDA,TSLA`. Body: the full daily briefing.
- **Workflow artifact** uploaded on every run (regardless of whether anything fired): `scan_results.json`, the captured `scan_output.txt`, and `trading_bot.log`. Open the run from the **Actions** tab and download from the bottom of the page. Retained 30 days.

### 13.7 Detection mechanism

`scan.py` prints a machine-readable `TAKE TRADE: <SYMBOL> <DIRECTION>` line for each fired signal. The workflow greps `scan_output.txt` for `^TAKE TRADE:` and only invokes the email step when at least one match is found. The symbol list is comma-joined into the subject.
