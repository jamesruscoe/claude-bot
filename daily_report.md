# Daily report — 2026-11-18T03:01:39+00:00

Market: **fx** · judge: deterministic · paper only

## Candidates (2)
- **GBPUSD=X** short score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)
- **USDCAD=X** long score 50 R:R 2.0 — TAKE/low/quarter · blocked: below calibrated FX_MIN_SCORE (50<85)

## Resolved this run (1)
- NZDUSD=X BREAKEVEN — 0.0R (sized)

## Ledger (running, sized R)
- 0 open · 2 closed · win rate n/a · total +0.01R

## Rejections by reason (all-time)
- no_setup: 703
- stale_feed: 99
- below calibrated FX_MIN_SCORE (50<85): 57
- regime_blocked: 21

> Paper only. Live bid/ask via an OANDA practice account is a documented TODO (see ARCHITECTURE.md / PROGRESS.md) — fills here use yfinance mid + assumed spread.