# Daily report — 2026-08-24T21:53:13+00:00

Market: **fx** · judge: deterministic · paper only

## Candidates (2)
- **EURUSD=X** long score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)
- **GBPUSD=X** long score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)

## Ledger (running, sized R)
- 0 open · 1 closed · win rate n/a · total +0.01R

## Rejections by reason (all-time)
- no_setup: 258
- stale_feed: 99
- below calibrated FX_MIN_SCORE (50<85): 29
- regime_blocked: 9

> Paper only. Live bid/ask via an OANDA practice account is a documented TODO (see ARCHITECTURE.md / PROGRESS.md) — fills here use yfinance mid + assumed spread.