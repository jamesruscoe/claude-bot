# Daily report — 2026-11-10T03:42:18+00:00

Market: **fx** · judge: deterministic · paper only

## Candidates (4)
- **NZDUSD=X** short score 100 R:R 2.0 — TAKE/low/quarter ← opened
- **EURUSD=X** short score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)
- **AUDUSD=X** short score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)
- **EURJPY=X** short score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)

## Opened this run (1)
- NZDUSD=X short @ 0.56139 (sl 0.5673 tp1 0.54957 tp2 0.54366, size quarter)

## Ledger (running, sized R)
- 1 open · 1 closed · win rate n/a · total +0.01R

## Rejections by reason (all-time)
- no_setup: 661
- stale_feed: 99
- below calibrated FX_MIN_SCORE (50<85): 55
- regime_blocked: 20

> Paper only. Live bid/ask via an OANDA practice account is a documented TODO (see ARCHITECTURE.md / PROGRESS.md) — fills here use yfinance mid + assumed spread.