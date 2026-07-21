# Range breakout — pre-registration (P1, first pattern)

Status: **parameters PRE-REGISTERED and frozen BEFORE any TRAIN measurement.** This
file exists so that "calibrate on TRAIN" cannot quietly become a sweep across the
parameter product. The values below were chosen a priori, with reasoning, and
committed before a single TRAIN number existed (see the commit that introduced this
file and `v2/patterns.py`). **Nothing here is fitted.** Judge off; the holdout stays
unread; the registered criterion (OANDA_ADAPTER_SCOPE.md) is untouched.

Range breakout was chosen as the first family because it is the only one with a
realistic path to `proven` on *forward* data (~12–25/yr estimate vs ~3–8/yr for
H&S): it is the pattern that actually tests whether the confidence architecture can
ever change a label. If even it can't clear the in-sample screen, that's a strong
signal about the whole approach.

## The parameter surface, and how few we fit (answer: zero)

Range breakout has an unusually large knob surface — what counts as a range, what
counts as a breakout, and what a failed breakout does. Each is a fork that could be
tuned on TRAIN. We fix **all** of them a priori and fit **none**, then measure once.

### What counts as a range
| Parameter | Value | Reasoning |
|---|---|---|
| `FX_RANGE_LOOKBACK` | **40 bars** | ~8 trading weeks of daily — long enough to form a multi-touch range, short enough to still be "current." |
| `FX_RANGE_MIN_TOUCHES` | **2 per boundary** | A level needs 2 touches to be a level, not a single wick. This is the *definitional minimum*, not a tuned choice — going higher (3+) would be a fit. |
| `FX_RANGE_EQ_ATR` | **0.5·ATR** | Swing highs within half an ATR of the ceiling (and lows of the floor) are "the same level." This single tolerance also **enforces flatness** — if the touches all sit within 0.5·ATR, the boundary is horizontal by construction, so there is no separate slope knob to tune. |
| width `R−S` bounds | **≥ 0.5·ATR and ≤ 4.0·ATR** (`FX_RANGE_MAX_ATR`) | Lower bound (reuses `EQ_ATR`, no new knob) = boundaries are distinct, not a degenerate point. Upper bound = a *contained* consolidation; wider than 4·ATR is a trend/triangle, not a range. |

### What counts as a breakout
| Parameter | Value | Reasoning |
|---|---|---|
| `FX_RANGE_BRK_ATR` | **0.25·ATR** | The current close must clear the boundary by a quarter-ATR — enough to filter a marginal poke, small enough not to demand a large move (which would gut frequency and enter late). |
| freshness | **prior close inside** | The breakout fires on the bar that *first* closes beyond the boundary (prior close was inside), so a sustained move doesn't re-fire every bar. Not a tunable value — it's the definition of the breakout bar under the global breakout-close entry rule. |

### What a failed breakout does
**No state machine.** A failed breakout is *not* modelled as "invalidate" vs "reset."
The trade's stop — `0.5·ATR` back toward the range via `compute_levels_fx` — is the
failure handler: if price closes beyond, we enter; if it falls back, the stop takes
us out as a normal loss. Modelling failed-breakout state would be extra knobs
(how-far-back counts as failed? how-long until reset?) with no a-priori-justified
values, so we don't. This is the single biggest knob we refuse to add.

### Entry / stop / TP (reused house risk model, not pattern-specific)
- **Entry = the breakout CLOSE** (global rule, PATTERN_SCOPE §2), passed to
  `compute_levels_fx` as a thin zone, worsened by the measured spread.
- **Stop = 0.5·ATR** below (long) / above (short) the entry (`SL_ATR_MULT`, existing).
- **TP1/TP2 = 2R/3R** of post-spread risk (existing). The structural measured move
  `R−S` is recorded as a sanity bound only, never as a second target system.

## Pre-registered acceptance — Gate 1 (in-sample screen, TRAIN only)

Measured **once**, on TRAIN data only (OANDA daily, pre-2021-01-01), net of the
**measured** bid/ask spread, resolved with the honest intrabar `walk_trade`. Report
`n`, frequency/yr, win rate, mean R, and the one-sided 95% bootstrap lower bound.

**Keep range breakout (carry it to forward paper) iff, on TRAIN:**
1. mean R net of measured spread is **non-negative**, AND
2. frequency is **material** (≳ 8/yr basket — enough that forward accumulation could
   plausibly reach the `n≥150` needed to ever leave `unproven`).

Otherwise **shelve it.** This is an in-sample screen, **not** validation — a positive
TRAIN number is the expected prior for a pattern whose geometry was chosen on the
same era of data, so passing Gate 1 only earns the pattern a forward trial at
probation size, never a `proven` label. Forward data alone sets confidence (P0).

**Reject-on-sight (unchanged, carried from the OANDA scope):** expectancy that rises
as n falls; an edge that appears only at one exact parameter value; any number found
by trying more than a handful of configs; the criterion being revised after a result
is seen. If the screen fails, it fails — we do not re-open the parameters to rescue it.

---

## Result (filled in AFTER the single measurement)

_Appended by `python run.py --pattern-calibrate range_breakout`._
