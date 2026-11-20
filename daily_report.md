# Daily report — 2026-11-20T02:28:53+00:00

Market: **fx** · judge: deterministic · paper only

## Candidates (2)
- **NZDUSD=X** short score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)
- **EURJPY=X** short score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)

## Ledger (running, sized R)
- 0 open · 2 closed · win rate n/a · total +0.01R

## Rejections by reason (all-time)
- no_setup: 719
- stale_feed: 99
- below calibrated FX_MIN_SCORE (50<85): 59
- regime_blocked: 21

> Paper only. Live bid/ask via an OANDA practice account is a documented TODO (see ARCHITECTURE.md / PROGRESS.md) — fills here use yfinance mid + assumed spread.