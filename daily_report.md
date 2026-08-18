# Daily report — 2026-08-18T21:47:00+00:00

Market: **fx** · judge: deterministic · paper only

## Candidates (2)
- **GBPUSD=X** long score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)
- **USDCAD=X** short score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)

## Ledger (running, sized R)
- 1 open · 0 closed · win rate n/a · total +0.00R

## Rejections by reason (all-time)
- no_setup: 232
- stale_feed: 99
- below calibrated FX_MIN_SCORE (50<85): 21
- regime_blocked: 7

> Paper only. Live bid/ask via an OANDA practice account is a documented TODO (see ARCHITECTURE.md / PROGRESS.md) — fills here use yfinance mid + assumed spread.