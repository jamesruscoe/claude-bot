# Daily report — 2026-07-27T22:33:32+00:00

Market: **fx** · judge: deterministic · paper only

## Candidates (3)
- **EURUSD=X** short score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)
- **AUDUSD=X** short score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)
- **EURJPY=X** long score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)

## Ledger (running, sized R)
- 0 open · 0 closed · win rate n/a · total +0.00R

## Rejections by reason (all-time)
- no_setup: 107
- stale_feed: 99
- below calibrated FX_MIN_SCORE (50<85): 9
- regime_blocked: 1

> Paper only. Live bid/ask via an OANDA practice account is a documented TODO (see ARCHITECTURE.md / PROGRESS.md) — fills here use yfinance mid + assumed spread.