# Multi-pattern FX detector — scope

Status: **decisions LOCKED 2026-07-21 (§8); P0 accounting plumbing in build.** No
threshold changes, judge off. The 2021–2026 daily holdout stays **unread** and the
registered criterion stays **locked** (see `OANDA_ADAPTER_SCOPE.md`). This document
leads with the architecture and the statistics, because — exactly as with the OANDA
work — the risk here is not writing the detectors, it is fooling ourselves with them.

## 0. The goal, and the trap it walks straight into

**Goal:** raise signal frequency by detecting several pattern *families* — not by
lowering `FX_MIN_SCORE`. Take a trade when **any enabled pattern** produces a
valid setup. The existing SMC path (OB retest, BOS retest) is unchanged; `FX_MIN_SCORE`
is untouched; nothing here loosens an existing gate.

**The trap:** every new pattern family is a new set of free parameters and a new
chance to discover a spurious edge. Five families, each with a handful of geometry
knobs tuned on the same history, is a large best-of-N surface — the exact machine
that already manufactured a fake +0.35R, +0.44R and +0.70R on this project. More
patterns is more *shots on goal at finding noise that looks like signal.*

So the deliverable is not "five detectors." It is the **accounting discipline that
lets us keep what works and drop what doesn't, per pattern, without lying to
ourselves**: per-pattern tagging, per-pattern expectancy, per-pattern out-of-sample
power, and a confidence score that is a function of *measured expectancy*, never of
how pretty the shape looks. The detectors are the easy part.

**The headline finding (derived in §5), stated up front so it frames everything:**
the daily holdout can fund `n ≥ 150` **in aggregate**, but **almost certainly not
per pattern** — and it is a single, one-shot resource that cannot honestly serve as
six independent per-pattern tests. Per-pattern verdicts therefore come from
*forward* accumulation over time, not from the holdout. This is why the
"confidence from own expectancy, else *unproven*" rule (§4.4) is load-bearing, not
a nicety: it is what makes turning these patterns on **safe** — a new pattern
cannot fabricate a track record it hasn't earned.

## 1. What the bot already computes (the arithmetic we build on)

Every definition below is pure OHLC arithmetic on primitives that already exist —
**no vision, no chart rendering.**

- `smc_detector._find_swings(bars, lookback=2)` → `(swing_high_idx, swing_low_idx)`:
  indices where `bars[i].h` is the local max (or `bars[i].l` the local min) over
  ±2 bars. This is the spine of every pattern here.
- `smc_detector.atr(bars, period=14)` → current ATR, used for all tolerances,
  "equal-level" bands, breakout buffers and stop distances (scale-free across pairs).
- `smc_detector.simple_bias(bars, window=60)` → bull/bear/neutral regime (the
  existing 50-bar-ish trend gate; reused as an optional per-pattern filter).
- `v2/levels.compute_levels_fx(direction, zone_low, zone_high, atr, price, …)` →
  pip/spread-aware entry, stop, TP1/TP2, R:R, lots. **Every pattern reuses this** —
  it converts a (direction, zone, stop hint) into the same honest, spread-charged
  levels the SMC path uses. No new risk math.
- `store.walk_trade` / `resolve_open_trades` → honest intrabar SL-first resolution.
  **Unchanged.** A pattern only has to emit (direction, entry zone, stop hint); the
  existing machinery resolves it faithfully.
- `store.open_trade(..., source=…)` and the `source` column (`store.py`) → the
  exact precedent for the new per-pattern `pattern` tag (§4.1).
- `oanda_baseline.bootstrap_mean_ci` → the one-sided bootstrap already used for the
  registered criterion; reused verbatim for per-pattern confidence (§4.4).

**Design invariant:** a "pattern detector" is a pure function
`detect_<name>(bars) -> PatternSetup | None`, where `PatternSetup` carries
`{pattern, direction, zone_low, zone_high, stop_hint, measured_move, key_levels}`.
It plugs in exactly where `score_setups` does today, so levels, resolution, sizing
and the ledger are all reused. Nothing downstream of detection changes shape.

## 2. Pattern catalog

Notation: `SH`/`SL` = swing-high/low indices from `_find_swings`; `hi(i)=bars[i].h`,
`lo(i)=bars[i].l`, `cl(i)=bars[i].c`; `A = atr(bars)`; `W` = per-pattern lookback
window (bars). All tolerances are ATR-scaled and **calibrated on TRAIN only**. Every
pattern's stop/TP is expressed so `compute_levels_fx` produces the levels — i.e. we
hand it a **zone** (entry band) and a **stop hint**, and keep TP as R-multiples
(`TP1_R_MULT=2`, `TP2_R_MULT=3`) with the pattern's **measured move** recorded
alongside as a sanity bound, never as a second target system.

Default parameter values below are **starting points for TRAIN calibration**, not
tuned results.

**Entry style — LOCKED globally, a priori: breakout-close for every pattern.** Entry
is the bar-close that confirms the pattern (neckline/level break), never a
wait-for-retest. This is chosen on principle (simpler, fewer missed entries, no
waiting for a retest that never comes), applied uniformly, and is **not** a per-
pattern fork tuned on TRAIN — six in-sample entry forks would be best-of-N by another
name (the retest-window arm already burned this project once). Forward data may later
show it's wrong; until then, one rule for all.

### 2.1 SMC Order-Block retest — EXISTING, UNCHANGED
Kept exactly as `detect_ob_retest`. Becomes simply "one registered pattern" in the
new accounting (tag `ob_retest`). No behaviour change; `FX_MIN_SCORE` still gates it.

### 2.2 SMC Break-of-Structure retest — EXISTING, UNCHANGED
Kept exactly as `detect_bos_retest` (tag `bos_retest`). The existing 0/50/100
dual-confluence score (OB+BOS agreeing = 100) stays meaningful **within these two
patterns' own bucket** (§4.3). Unchanged.

### 2.3 Double bottom (long) / Double top (short)
**Geometry (double bottom):** within `W` (default 40), find the two most-recent
swing lows `L1 < L2` with an intervening swing high `N` (the neckline), such that:
- `|lo(L1) − lo(L2)| ≤ eq_tol·A` (bottoms roughly equal; `eq_tol` default 0.5)
- `hi(N) − min(lo(L1),lo(L2)) ≥ amp_min·A` (real amplitude; `amp_min` default 1.5)
- `sep_min ≤ (L2 − L1) ≤ sep_max` bars (default 5…30)
- `L2` is recent (formed within the last `recency` bars, default 5)

**Entry trigger:** `cl(now) > hi(N)` (neckline breakout). Entry zone = a thin band
at `hi(N)` (breakout-retest style, matching the OB/BOS zone convention).
**Stop hint:** `min(lo(L1),lo(L2)) − SL_ATR_MULT·A`.
**TP:** R-multiples from that stop via `compute_levels_fx`; **measured move**
`= hi(N) − min(lo(L1),lo(L2))` recorded as sanity bound.
**Double top:** exact mirror on swing highs; neckline = intervening swing low;
trigger `cl(now) < lo(N)`; short.
**Rough frequency (daily, 9-pair basket):** ~8–16/yr combined. *Estimate — measure on TRAIN.*

### 2.4 Head-and-shoulders (short) / Inverse H&S (long)
**Geometry (inverse H&S, long):** within `W` (default 60), find swing lows
`LS, H, RS` and intervening swing highs `T1` (between LS,H) and `T2` (between H,RS),
ordered `LS < T1 < H < T2 < RS`, with:
- `lo(H) < lo(LS)` and `lo(H) < lo(RS)` (head is the extreme)
- `|lo(LS) − lo(RS)| ≤ sh_tol·A` (shoulders roughly level; `sh_tol` default 0.6)
- neckline = line through `hi(T1),hi(T2)`; require `|slope|·bars ≤ neck_tol·A`
  (near-horizontal to mildly sloped; `neck_tol` default 1.0)
- `neckline − lo(H) ≥ amp_min·A` (`amp_min` default 1.5)

**Entry trigger:** `cl(now) >` neckline evaluated at `now`. Entry zone = band at the
neckline level. **Stop hint:** `lo(RS) − SL_ATR_MULT·A` (tight; a wider variant at
`lo(H)` is a config option, off by default). **TP:** R-multiples; **measured move**
`= neckline(at H) − lo(H)`. **H&S (short):** mirror on swing highs. **Rough
frequency:** ~3–8/yr combined — the **rarest** family. *Estimate — measure on TRAIN.*

### 2.5 Triangles — ascending / descending / symmetrical
**Geometry:** within `W` (default 50), take the last `k` swing highs and last `k`
swing lows (`k ≥ 2`, default 3). Least-squares fit a line to each set over
`(index, price)` → `slope_hi, slope_lo`, with residuals ≤ `fit_tol·A`.
- **Ascending:** `|slope_hi|·A_bars ≤ flat_tol` (flat resistance) **and**
  `slope_lo > rise_tol` (rising support). Trigger `cl(now) >` resistance level. Long.
- **Descending:** `slope_hi < −rise_tol` (falling) **and** `|slope_lo| ≤ flat_tol`
  (flat support). Trigger `cl(now) <` support level. Short.
- **Symmetrical:** `slope_hi < −conv_tol` **and** `slope_lo > conv_tol` (converging),
  **and** lines still un-crossed (apex ahead). Trigger: `cl(now)` beyond the nearer
  line by `brk_buf·A`; direction = breakout side.
- **Convergence guard (all three):** the high/low lines must be *narrowing* —
  `|res(now) − sup(now)| < |res(W₀) − sup(W₀)|`.

**Entry zone:** band at the broken line. **Stop hint:** opposite line / last
opposing swing ± `SL_ATR_MULT·A`. **TP:** R-multiples; **measured move** = triangle
height at its widest, projected from breakout. **Rough frequency:** ~8–16/yr
combined (clean, rule-passing triangles are much rarer than eyeballed ones).
*Estimate — measure on TRAIN.*

### 2.6 Range breakout
**Geometry:** within `W` (default 40), cluster swing highs within `eq_tol·A` → level
`R` (≥2 touches), swing lows within `eq_tol·A` → level `S` (≥2 touches), require the
band contained (`R − S ≤ range_max·A`, default 4) and **both boundaries roughly
horizontal** (`|slope| ≤ flat_tol` — this is what distinguishes a *range* from a
triangle or a single-level BOS).
**Entry trigger:** `cl(now) > R + brk_buf·A` (long) or `cl(now) < S − brk_buf·A`
(short); the buffer (`brk_buf` default 0.25) filters false pokes.
**Stop hint:** back inside — the broken boundary ∓ `SL_ATR_MULT·A`. **TP:**
R-multiples; **measured move** `= R − S` projected from the broken boundary.
**Rough frequency:** ~12–25/yr — the **most frequent** family, but with the most
false breaks (hence the buffer and the per-pattern expectancy gate). *Estimate — measure on TRAIN.*

**Deliberate overlap.** Range breakout, the flat side of a triangle, and BOS retest
can all fire on the same structure. That is fine and expected — it is handled by the
confluence/dedup rules (§4.3, §4.5), not by trying to make the detectors mutually
exclusive.

## 3. Non-negotiable architecture

### 3.1 Per-pattern ledger tagging + per-pattern expectancy (requirement 1)
Add a `pattern TEXT NOT NULL DEFAULT 'ob_retest'` column to `trades`, via the same
lightweight migration list already used for the `source` column (`store.py`). Every
opened trade records the pattern that triggered it. Add `trades_by_pattern()`
(mirroring `trades_by_source`) and a `--pattern-report` that emits, **per pattern**:
`n_resolved, win_rate, mean_R (net of measured spread), one-sided 95% bootstrap
lower bound, total_R, freq/yr`. Aggregate is *also* shown, but the per-pattern table
is the point — it is what lets us keep the winners and disable the losers on
evidence. Attribution rule for multi-pattern entries in §4.3.

### 3.2 Per-pattern enable/disable (requirement 2)
A registry `FX_PATTERNS = {"ob_retest": True, "bos_retest": True, "double_top": False,
"double_bottom": False, "hns": False, "inv_hns": False, "triangle_asc": False,
"triangle_desc": False, "triangle_sym": False, "range_breakout": False, …}`, each
overridable by env (`BOT_FX_PATTERN_<NAME>=0/1`). **Existing SMC patterns default ON
(behaviour unchanged); every new pattern defaults OFF** — consistent with the
standing rule that everything new ships behind a flag in the safe/off state. Each
pattern's geometry parameters (§2) are also config-exposed for TRAIN calibration.

### 3.3 Scoring interaction — does a pattern score 100 alone? (requirement)
**No.** The discrete `0/50/100` score was specific to OB+BOS confluence and it stays
**only** inside that pair's bucket (OB alone = 50, OB+BOS agreeing = 100, still gated
by `FX_MIN_SCORE` — unchanged). For the wider world, **validity is binary per
pattern** (the geometry either fires or it doesn't), and **"confidence" (§4.4)
replaces "score" as the quality signal** — derived from measured expectancy, not
shape. So:
- A new pattern does **not** route through `FX_MIN_SCORE` and does not need a score
  of 100. If its geometry is satisfied and it is enabled, it is a valid setup.
- **Confluence is a bonus, not a gate.** When ≥2 patterns fire the same
  symbol+direction, that agreement raises confidence (and can raise probationary
  size), but no single pattern needs confluence to trade. This is exactly the
  "take the trade when *any* pattern produces a valid setup" instruction.
- `FX_MIN_SCORE` is **not changed and not bypassed** — it simply governs only the
  OB/BOS pattern, as today.

### 3.4 Confidence from measured expectancy, never shape (requirement)
Confidence for a firing pattern `p` is a pure function of **`p`'s own realized
track record**, computed from the honest ledger with `bootstrap_mean_ci`:
`n_p` (resolved trades of pattern `p`), `mean_R_p`, and the one-sided 95% lower
bound `LB_p`. Tiers:

| Condition | Confidence | Size (via existing graduated sizing) |
|---|---|---|
| `n_p < N_CONF_MIN` (default 30) | **`unproven`** | probation minimum |
| `N_CONF_MIN ≤ n_p < 150` | `provisional (n=…)`, soft = clamp(`LB_p`) | small, scaling with `LB_p` |
| `n_p ≥ 150` and `LB_p > 0` | `proven`, scales with `LB_p` | full graduated |
| `n_p ≥ 150` and `LB_p ≤ 0` | `not positive` | → candidate for auto-disable |

`N_CONF_MIN` is **LOCKED at 30**, and `provisional` must not be read as "working":
at `n = 30` the standard error of the mean is ≈ `1.7/√30 ≈ 0.31R`, i.e. the whole
plausible-edge range is wider than any edge we'd expect. **Provisional means
"barely more than unproven, at small size" — not validated.** Real validation is
`n ≥ 150` on *forward* data.

Hard rules:
- **Confidence is never a function of shape quality.** A textbook H&S with 4 trades
  is `unproven`, not "high" — because `n=4`. This is the single most important line
  in the document: it is what stops a pretty new pattern from masquerading as a
  proven one.
- **Confidence is FORWARD-ONLY. No TRAIN-seeded prior. LOCKED.** Patterns are
  *calibrated* on train, so their train expectancy is in-sample and inflated by
  construction; importing it into confidence would import exactly that inflation into
  the number the email shows you. **Train sets geometry; forward sets confidence —
  kept strictly separate.** Consequence: at launch **every new pattern is `unproven`
  and trades at probation size**, earning confidence only as its forward record
  grows. Honest cold-start; reuses the graduated probationary sizing in `brain.py`.
- A pattern that reaches `n ≥ 150` with `LB_p ≤ 0` is flagged for disable: this is
  the "drop what doesn't work" half of requirement 1, made automatic.
- **PAPER vs REAL MONEY — the load-bearing caveat.** Dropping `FX_MIN_SCORE` as the
  gate in favour of binary-validity + confidence-based *sizing* moves protection from
  "don't take it" to "take it tiny." That is defensible **for paper evidence-
  gathering only** — a tiny-size loser from an unproven pattern still teaches us
  something, which is the whole point of turning them on. **For real money,
  `unproven` means DO NOT TRADE IT AT ALL** (probation size = 0, not "small"). This
  must be explicit in code and in the email, because the first alert from a brand-new
  pattern will otherwise look exactly like a proven one with a smaller number
  attached. `FX_MIN_SCORE` has been the main thing keeping junk out; we are only
  relaxing that for paper.

### 3.5 Correlation cap across the wider signal stream (requirement)
More patterns ⇒ more simultaneous signals ⇒ concentration risk. Mitigations, all on
the **open-trade stream regardless of which pattern generated each**:
- **Existing `FX_MAX_PER_CCY`** already caps trades pushing one currency the same
  way, and it operates on open trades, so it **already spans patterns** (a USD-long
  from a range break and a USD-long from an OB both count). With a wider stream it
  will bind more often — that is the intended conservative direction, not a bug.
- **Per-(symbol, direction) dedup across ALL patterns:** never open two tickets on
  the same pair+direction because two patterns fired; collapse to **one** trade with
  a confluence tag (§4.3). Mirrors the existing "already in this symbol+direction"
  dedup.
- **New total-open cap `FX_MAX_OPEN = 5`** (LOCKED) to bound portfolio exposure as
  frequency rises, **plus a per-pattern sub-cap `FX_MAX_PER_PATTERN = 2`** (LOCKED):
  six patterns can cluster hard in a trending regime — three all firing long-USD is
  one bet wearing three tickets, which the per-currency cap only partly catches, and
  the sub-cap stops any single detector flooding the ledger and skewing the aggregate.
  Caps only ever block, so both are safe-by-default.

### 3.6 Email states the pattern and its confidence (requirement)
Extends the FX alert just shipped (PR #8). Subject:
`FX SIGNAL: GBPUSD LONG @ 1.2840  [double_bottom · unproven]`. Body adds
`Pattern: double_bottom` and `Confidence: unproven (n=7 forward)`; on confluence it
lists the primary pattern plus the agreeing ones. The alert still fires only on
**opened** trades, so the email stream stays identical to the ledger (the PR #8
invariant), now annotated with which pattern and how much we actually trust it.

### 3.7 Multi-pattern entry resolution & attribution (supporting 3.1/3.3/3.5)
Per symbol per scan: run every enabled detector, collect all firing setups, then:
1. Group by `(symbol, direction)`; **one trade max per group** (dedup).
2. **Primary pattern** = the highest-confidence firing pattern in the group (ties
   broken by a fixed registry priority so it is deterministic; when all are
   `unproven`, priority alone decides). The trade's `pattern` tag = primary.
3. `confluence = [other firing patterns]`, recorded in a side field for a separate
   confluence analysis. **Per-pattern expectancy (§3.1) is computed on the PRIMARY
   tag only**, so a trade is never double-counted across patterns.
4. Opposing setups on the same symbol (one long, one short) → **no trade** (conflict),
   logged — mirrors the existing `conflicting_setups` rejection.

## 4. Frequency & power — can the holdout fund n ≥ 150? (requirement)

Anchors we measured on OANDA daily: dual-confluence ≈ **18/yr** basket; the daily
holdout (2021→2026, ~5.5 yr) holds ≈ **97** dual-confluence setups. To reach
`n ≥ 150` in that holdout a stream needs ≈ **27/yr**.

Rough combined estimate (wide error bars — **to be measured on TRAIN, not trusted
here**):

| Pattern family | Est. freq/yr (basket) | Est. holdout n (~5.5 yr) | Funds n≥150 solo? |
|---|--:|--:|:--:|
| OB/BOS dual (existing) | ~18 | ~97 | no |
| Double top/bottom | ~8–16 | ~44–88 | no |
| H&S / inverse | ~3–8 | ~17–44 | **no** |
| Triangles (all 3) | ~8–16 | ~44–88 | no |
| Range breakout | ~12–25 | ~66–138 | borderline/no |
| **Aggregate (all enabled)** | **~50–85** | **~275–470** | **yes (aggregate only)** |

**Two conclusions, both decisive:**

1. **Per-pattern, the daily holdout does not fund `n ≥ 150` for essentially any
   single family** (range breakout is borderline at best; H&S is nowhere close). The
   aggregate clears it comfortably, but per-pattern verdicts at the locked criterion
   are **not powered on daily data.**
2. **The holdout is one-shot and shared.** Evaluating six patterns on the same
   locked holdout is six looks — multiple comparisons. Controlling that (e.g.
   Bonferroni) makes each per-pattern bar *stricter*, eroding the already-insufficient
   per-pattern power further. The holdout can honestly answer **one** pre-registered
   question — a portfolio composite, or one top pattern — **not six**.

**Honest resolution (this is the spine of the plan) — LOCKED:** per-pattern
validation comes from **forward accumulation**, not the holdout. Each pattern starts
`unproven`, trades at probation size, and earns confidence from its *own forward*
expectancy as `n_p` grows (§3.4).

**Do NOT spend the holdout now — and NOT on a portfolio composite.** A composite of
six freshly-fit patterns could pass on one strong pattern carrying five duds, and you
would have no way to tell which — you would have burned your only clean out-of-sample
resource to learn nothing separable. Instead, a **two-stage design**: let forward
accumulation **nominate** a single candidate (a pattern that looks genuinely positive
on its own forward record), *then* spend the holdout on that **one** pre-registered
question. Forward data and holdout data are independent, so selecting the candidate on
forward performance does **not** contaminate the holdout — this is a legitimate
two-stage test, and it keeps the asset intact until there is something worth spending
it on. Until then the holdout stays unread and the criterion stays locked.

## 5. Calibration & out-of-sample discipline (requirement 3)

Carried over verbatim from `OANDA_ADAPTER_SCOPE.md` — the discipline does not get
weaker because there are more patterns; it gets **more** important:
- **All geometry parameters (§2) are chosen on TRAIN (pre-2021-01-01) only.** The
  holdout is not opened, plotted, aggregated, or peeked at.
- **Pre-register each pattern's definition + parameters BEFORE any holdout look.**
  Write them into config and commit, dated, before evaluation — the same trade-blind
  commit-ordering used for the split boundary.
- **Reject-on-sight (per pattern):** expectancy that rises as `n` falls; an edge that
  appears at exactly one parameter value; any result found by trying more than a
  handful of configs; **and the criterion (metric, n, bar) being revised after a
  result is seen.** With multiple patterns the first three are far more likely — a
  detector that only works at `eq_tol=0.47` is noise.
- The registered criterion (`n ≥ 150`, one-sided 95% bootstrap lower bound > 0, net
  of measured spread) is **unchanged and does not move.** Per §4 it applies at the
  portfolio level for any single holdout spend; per-pattern it is the *target* a
  pattern must reach via forward data before it is called `proven`.

## 6. Phased plan, with gates

- **P0 — accounting plumbing, no behaviour change, no new pattern.** `pattern` ledger
  column (existing OB/BOS trades tagged from their `setups`) + `trades_by_pattern` +
  `--pattern-report`; per-pattern config registry; forward-only confidence/`unproven`
  engine; email fields (pattern + confidence); the `FX_MAX_OPEN` / `FX_MAX_PER_PATTERN`
  caps (inert while only OB/BOS runs). Confidence is *reported*, not yet wired to
  sizing (that activates when patterns turn on). Acceptance: existing numbers
  reproduce **bit-for-bit** (selftest + suite green), because nothing in the
  decision/size/detector path changes. The `PatternSetup` detector-protocol refactor
  is deferred to the start of P1, where it is actually needed.
- **P1 — implement + TRAIN-calibrate each pattern, one at a time.** Introduce the
  `PatternSetup` protocol + registry dispatch; then per pattern: pre-register
  parameters, measure TRAIN frequency + expectancy net of measured spread via the
  replay harness. *Gate 1 (per pattern, in-sample screen):* keep a pattern only if
  TRAIN expectancy is non-negative net of spread **and** frequency is material;
  otherwise shelve. Screening, **not** validation.
- **P2 — forward accumulation (no holdout access).** Enabled patterns trade at
  probation size on the live cron; per-pattern confidence grows from *forward*
  expectancy; `not positive` patterns auto-flag for disable; `proven` patterns scale
  up. This is the real per-pattern verdict engine, and it runs for free on the cron
  already accumulating un-selected forward trades. **The holdout is not spent here.**
- **P3 — (only when forward nominates a candidate) single holdout evaluation.** When
  forward accumulation surfaces one pattern that looks genuinely positive on its own
  out-of-sample record, spend the holdout on **that one** pre-registered question,
  **once**. Selection-on-forward does not contaminate the holdout (independent data).
  No portfolio composite, no six-way look, no iterating after. Until a candidate is
  nominated, the holdout stays unread.

## 7. Non-goals / explicit guards

- **No `FX_MIN_SCORE` change; no lowering of any existing gate.** Frequency rises
  only from new pattern families, never from a looser threshold.
- **No confidence from shape quality** — expectancy + sample size only; insufficient
  sample ⇒ `unproven`.
- **Holdout stays unread; criterion stays locked; judge stays off.**
- New patterns **off by default**; existing SMC path bit-for-bit unchanged.
- Reuse levels, resolution, sizing, ledger, correlation cap, and the PR #8 mailer —
  **no new risk math, no new resolution logic, no new email system.**
- Not a route to "restore +0.35R." If the honest per-pattern ledger says a family
  has no edge, it gets disabled. Dropping losers is a feature, not a failure.

## 8. Decisions — LOCKED 2026-07-21

All four resolved before P0; recorded here so they are not silently revisited.

1. **`N_CONF_MIN = 30`, confidence FORWARD-ONLY** (no train-seeded prior). Train sets
   geometry; forward sets confidence. `provisional` at n=30 is explicitly *not*
   validation (SE ≈ 0.31R) — see §3.4.
2. **Do NOT spend the holdout now, and not on a portfolio composite.** Two-stage:
   forward accumulation nominates a single candidate, then the holdout answers that
   one pre-registered question (independent data ⇒ no contamination) — see §4, §6-P3.
3. **`FX_MAX_OPEN = 5`, `FX_MAX_PER_PATTERN = 2`** — see §3.5.
4. **Entry = breakout-close for every pattern, chosen a priori**, not tuned per
   pattern on TRAIN — see §2.

A fifth, standing decision (the load-bearing caveat, §3.4): relaxing `FX_MIN_SCORE` to
binary-validity + confidence-based *sizing* is **for paper only**. For real money,
`unproven` = do not trade (size 0).

---

**One line to keep it honest:** more patterns is more ways to find noise. The
per-pattern ledger + expectancy-derived confidence + `unproven`-by-default is the
whole deliverable; the geometry is arithmetic we already own.
