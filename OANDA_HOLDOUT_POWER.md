# OANDA holdout POWER check — expected trade count only (NO outcomes)

Answers ONE question before Phase C: does the 2021-01-01+ holdout contain enough dual-confluence setups to reach the registered **n >= 150**? This counts qualifying entries only — **no** TP/SL resolution, **no** R, **no** win/loss is computed, so the holdout is NOT burned by running it.

| Pair | Train dual entries | Holdout dual entries | Holdout span (yrs) |
|------|-------------------:|---------------------:|-------------------:|
| EURUSD=X | 40 | 8 | 5.54 |
| GBPUSD=X | 39 | 12 | 5.54 |
| USDJPY=X | 52 | 8 | 5.54 |
| USDCHF=X | 45 | 12 | 5.54 |
| AUDUSD=X | 51 | 16 | 5.54 |
| USDCAD=X | 47 | 12 | 5.54 |
| NZDUSD=X | 48 | 13 | 5.54 |
| EURGBP=X | 27 | 4 | 5.54 |
| EURJPY=X | 35 | 12 | 5.54 |

**Holdout total: 97 dual-confluence entries** over ~5.5 years (~18/yr).

_Method check: this outcome-blind counter reports **384** dual entries on TRAIN; the resolved-trade replay opened 360 (358 resolved + 2 still open) — agreement to ~7% (the small gap is tail still-open de-dup handling, not a systematic bias). Close enough to trust the counter as a sample-size estimate; and the holdout margin below dwarfs it._

## Verdict

**n = 97 < 150 — the holdout CANNOT power the registered test.** This is **Outcome 4** (insufficient n to decide), reached BEFORE seeing any result — the only clean way to reach it. Per the locked protocol: do NOT lower the bar and do NOT call a positive-but-underpowered result 'marginal'. Daily granularity cannot power the decision; this is exactly the finding that earns intraday (Phase D) — forward paper / finer bars to reach n>=150 — rather than a fishing expedition.
