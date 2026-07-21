# CALIBRATION_v2 — FX detector recalibrated from scratch

Fresh walk-forward after the detector changes (retest window k=3, vol-scaled
impulse = 1.5×ATR%, close-to-close impulse). Raw, unsized R; live detectors +
honest intrabar SL-first resolution, on current data (through 2026-07). Scores
are still discrete {50, 100}, so `FX_MIN_SCORE` in (50,100] selects the
dual-confluence (score==100) set — **85 == 100 operationally**. No threshold
changed; `FX_MIN_SCORE` stays 85. Judge stays off.

## Bottom line

**The three changes AS COMBINED weakened the edge**: dual-confluence expectancy
fell to **+0.17R (n=76)** from **+0.35R (n=89)**. Under 1.5× spread it drops to
**+0.11R**. The ≥50 bucket went slightly negative (−0.01R). This is stated
plainly — the small cadence change is **not** a win.

The ablation below decomposes it, and two follow-up experiments (c2c confound
test + ATR-multiplier sweep) then overturn the naive read: **the c2c drop is the
*correct* measurement exposing that much of the o2c edge was the degenerate-open
artifact**, and **vol-scaling's apparent gain is selection on a shrinking sample,
not a plateau**. Net: do not adopt these; the real fix is real OHLC (OANDA) + a
second setup type. See "Follow-up experiments" and "Verdict" below.

## Ablation — one change at a time (dual-confluence, 1.0× spread)

| Config | n | win% | avg R | /week |
|--------|--:|-----:|------:|------:|
| **1) OLD detector** (win=1, flat 0.8%, o2c) | 89 | 25% | **+0.35** | 0.68 |
| 2) + retest window=3 only | 92 | 21% | +0.32 | 0.70 |
| **3) + vol-scale impulse only** (win=1) | 38 | 39% | **+0.70** | 0.29 |
| **4) + close-to-close impulse only** (win=1, flat) | 91 | 20% | **+0.07** | 0.69 |
| 5) **ALL THREE** (as implemented) | 76 | 25% | **+0.17** | 0.58 |

- **Close-to-close (#3 fix) is net-negative** — +0.35R → +0.07R. My first guess
  (a window-width confound) was **wrong** — see the confound test below. The
  degradation is the price reference itself, and that is the important finding,
  not a reason to switch c2c off.
- **Vol-scaled impulse (#2 fix) is the real improvement** — +0.35R → **+0.70R**
  and win rate 25% → 39%, by being *more* selective (higher effective threshold
  on hot pairs). But frequency **halves** (0.68 → 0.29/week ≈ one dual-confluence
  every ~3.5 weeks).
- **Retest window k=3 (#1 fix) is ~neutral** — +0.35R → +0.32R, tiny cadence
  gain. Harmless; not the lever it looked like.
- Combined, c2c's damage cancels most of vol-scaling's gain → +0.17R.

## Effective impulse threshold per pair (vol-scaled = 1.5 × ATR%)

| Pair | price | ATR% | effective impulse | (old flat) |
|------|------:|-----:|------------------:|-----------:|
| EURUSD=X | 1.1410 | 0.497% | **0.746%** | 0.800% |
| GBPUSD=X | 1.3387 | 0.661% | **0.992%** | 0.800% |
| USDJPY=X | 163.00 | 0.405% | **0.608%** | 0.800% |
| USDCHF=X | 0.8125 | 0.667% | **1.000%** | 0.800% |
| AUDUSD=X | 0.7009 | 0.686% | **1.029%** | 0.800% |
| USDCAD=X | 1.4102 | 0.444% | **0.665%** | 0.800% |
| NZDUSD=X | 0.5834 | 0.853% | **1.280%** | 0.800% |
| EURGBP=X | 0.8522 | 0.402% | **0.603%** | 0.800% |
| EURJPY=X | 185.95 | 0.468% | **0.702%** | 0.800% |

## Threshold curve — basket (ALL THREE, 1.0× spread)

| Threshold | resolved | /week | win% | avg R |
|-----------|---------:|------:|-----:|------:|
| ≥50 (all) | 402 | 3.05 | 18% | −0.01 |
| =100 / ≥85 (dual) | 76 | 0.58 | 25% | +0.17 |

## Per-pair (ALL THREE, 1.0× spread)

| Pair | n≥50 | win% | avgR ≥50 | n(dual) | avgR(dual) |
|------|-----:|-----:|---------:|--------:|-----------:|
| EURUSD=X | 36 | 17% | −0.08 | 10 | −0.28 |
| GBPUSD=X | 50 | 23% | +0.06 | 7 | +0.65 |
| USDJPY=X | 42 | 33% | +0.51 | 5 | +0.27 |
| USDCHF=X | 50 | 22% | +0.11 | 13 | +0.49 |
| AUDUSD=X | 51 | 16% | −0.06 | 11 | −0.08 |
| USDCAD=X | 28 | 10% | −0.20 | 8 | −0.45 |
| NZDUSD=X | 50 | 10% | −0.31 | 7 | −0.66 |
| EURGBP=X | 47 | 12% | −0.13 | 7 | +0.14 |
| EURJPY=X | 48 | 18% | −0.02 | 8 | +1.41 |

_(Per-pair dual samples are tiny (n≤13) — individually noise; shown for completeness.)_

## Dual-confluence: BEFORE vs AFTER

| | resolved | /week | win% | avg R |
|-|---------:|------:|-----:|------:|
| BEFORE (old detector) | 89 | 0.68 | 25% | **+0.35** |
| AFTER (all three) | 76 | 0.58 | 25% | **+0.17** |

_(≥50 before: 404, 3.07/wk, 18%, +0.05R. After: 402, 3.05/wk, 18%, −0.01R.)_

## 1.5× spread stress test

| slice | avg R @1.0× | avg R @1.5× |
|-------|-----------:|-----------:|
| dual (≥85), all three | +0.17 | +0.11 |
| ≥50, all three | −0.01 | −0.02 |

_(For reference, vol-scale-only dual at 1.0× was +0.70R — that config's spread stress was not run in this pass; re-run before relying on it.)_

## Follow-up experiments (the two traps)

### 1. The c2c "confound" — tested and REFUTED

Hypothesis: c2c anchors at `close[start-1]`, one bar earlier than o2c's
`open[start]`, so c2c may have widened the impulse window rather than changed the
price reference. Isolated by exposing `impulse_max_len` (dual, flat 0.8%, win=1):

| Config | n | win% | avg R |
|--------|--:|-----:|------:|
| A) o2c, max_len=3 (baseline) | 89 | 25% | +0.35 |
| B) o2c, max_len=4 (widen window +1, same price ref) | 100 | 28% | **+0.44** |
| C) c2c, max_len=3 (as implemented) | 91 | 20% | +0.07 |
| D) c2c, max_len=2 (window-held to match o2c's span) | 74 | 19% | +0.10 |

**Refuted.** Widening o2c's window *helps* (+0.35 → +0.44); holding the window
fixed and switching only o2c→c2c *still collapses* the edge (+0.35 → +0.10). The
degradation is the **close-to-close price reference**, not the extra bar.

Implication (the point I had backwards): c2c is the *more correct* measurement —
Yahoo's opens are degenerate, so o2c is the artifact. That ~2/3 of the
dual-confluence edge evaporates under the faithful measurement means **much of
the +0.35R was the artifact, not a market inefficiency.** The fix is not
`BOT_FX_IMPULSE_C2C=0` (that re-embeds the artifact) — it's real OHLC.

### 2. Vol-scale ATR-multiplier sweep — no plateau

| multiplier | n | win% | avg R | /week |
|-----------:|--:|-----:|------:|------:|
| 1.25× | 69 | 32% | +0.49 | 0.52 |
| 1.50× | 38 | 39% | +0.70 | 0.29 |
| 1.75× | 15 | 50% | +0.98 | 0.11 |

Monotonic — expectancy rises **only as the sample shrinks** (to n=15). That is the
signature of selecting a smaller, higher-variance tail, **not** a stable edge. No
plateau at meaningful n. The +0.70R at 1.5× was best-of-N inflation.

## Verdict — do not adopt; the edge is weaker than o2c implied

- **As combined, the three changes weakened the edge** (+0.35R → +0.17R; +0.11R
  at 1.5× spread). Stated plainly.
- **The most faithful measurement (c2c) puts dual-confluence at ~+0.10R**, not
  +0.35R. A large part of the backtested edge was the degenerate-open artifact.
- **Vol-scaling's +0.70R does not survive scrutiny** — it's a monotonic
  frequency/quality tradeoff on tiny samples (n=15–69), consistent with
  selection on noise.
- **Retest window** is roughly neutral-to-slightly-helpful; not a lever.

No threshold changed, nothing auto-adopted, all knobs left at their branch
defaults for inspection. **Recommendation: stop tuning the OB/BOS detector on
Yahoo daily data** — it has degenerate opens (breaks the impulse test), small
in-sample samples (n≈15–90), and we're now several forks deep into best-of-N
selection. The two real moves are the ones already on the roadmap:
1. **Real OHLC from OANDA practice** — settles the c2c question with data that
   isn't broken, and gives honest impulses. This is the prerequisite for
   trusting any of the above.
2. **A second setup type** for frequency — vol-scaling even in its best case runs
   ~0.29 trades/week (one per ~3.5 weeks); it fixes quality, not frequency.
