# Daily report — 2026-11-04T01:33:30+00:00

Market: **fx** · judge: deterministic · paper only

## Candidates (1)
- **USDCHF=X** short score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)

## Ledger (running, sized R)
- 0 open · 1 closed · win rate n/a · total +0.01R

## Rejections by reason (all-time)
- no_setup: 642
- stale_feed: 99
- below calibrated FX_MIN_SCORE (50<85): 50
- regime_blocked: 18

> Paper only. Live bid/ask via an OANDA practice account is a documented TODO (see ARCHITECTURE.md / PROGRESS.md) — fills here use yfinance mid + assumed spread.