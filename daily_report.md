# Daily report — 2026-08-19T21:50:16+00:00

Market: **fx** · judge: deterministic · paper only

## Candidates (4)
- **EURUSD=X** long score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)
- **AUDUSD=X** long score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)
- **USDCAD=X** short score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)
- **NZDUSD=X** long score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)

## Resolved this run (1)
- EURJPY=X EXPIRED — 0.012R (sized)

## Ledger (running, sized R)
- 0 open · 1 closed · win rate n/a · total +0.01R

## Rejections by reason (all-time)
- no_setup: 236
- stale_feed: 99
- below calibrated FX_MIN_SCORE (50<85): 25
- regime_blocked: 8

> Paper only. Live bid/ask via an OANDA practice account is a documented TODO (see ARCHITECTURE.md / PROGRESS.md) — fills here use yfinance mid + assumed spread.