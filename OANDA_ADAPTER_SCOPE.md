# OANDA practice adapter — scope

Status: **proposal / not built.** This document scopes a real-OHLC data adapter.
It deliberately leads with methodology, not code, because the risk here is not
technical — it's statistical.

## 0. Why this exists, and what it is NOT

The FX detector work reached a genuine, valuable, **negative** result
(`CALIBRATION_v2.md`): the dual-confluence "edge" of +0.35R was substantially a
**measurement artifact** of Yahoo's degenerate daily opens. Measured faithfully
(close-to-close), it is **~+0.10R at a 19% win rate, before real costs** — and a
+0.05R edge already degraded under spread stress earlier. The honest position is
not "the edge is smaller than we thought"; it is:

> **We do not currently have evidence of an edge.**

OANDA is **not a fix that restores +0.35R.** It will not make the strategy
profitable. It is one thing only: **the instrument that lets us find out whether
there is anything there at all, on data whose opens are real.** Pre-commit, now,
to accepting whichever of these three outcomes the clean data returns:

| Outcome on clean data | Action |
|---|---|
| **Real edge** — registered n reached, one-sided lower CI bound > 0 | Proceed carefully — small size, long forward paper test. |
| **Marginal** — registered n reached, but CI straddles 0 | Tiny size, long forward test, or shelve. Do **not** talk yourself into it. |
| **No edge** — registered n reached, point estimate ≤ 0 | Stop. Time spent, **no money lost** — a win relative to the alternative. |
| **Insufficient n to decide** — cannot reach registered n in obtainable data | **Stop and reconsider granularity.** Do NOT file this as "marginal" and do NOT loosen the bar — too few trades to ever prove itself *is* the finding. |

If you are not willing to accept outcomes 3 **and 4** before looking, do not run
the test. Given ~0.68 dual-confluence trades/week, **outcome 4 is the one to
weight most heavily** — the power math in §1 shows why.

## 1. Prime directive: the out-of-sample guard (build this FIRST)

Real intraday data means **far more bars and far more knobs** — granularity,
session windows, session-relative levels, intrabar entries, more setup variants.
That is more surface for exactly the best-of-N selection that just produced a
fake +0.70R (monotonic expectancy as n shrank to 15). **The guard must exist
before any parameter is fit, or the clean data will be laundered into the same
mistake.**

The protocol, non-negotiable:

1. **Split by time, up front.** Partition available history into **TRAIN** (older
   ~70%) and a **LOCKED HOLDOUT** (most recent ~30%, a fixed date range). Write
   the split boundary into config as a constant. The holdout is not opened,
   plotted, aggregated, or peeked at during any tuning.
2. **Pre-register the acceptance criterion — and know its statistical power
   before you write it down.** The outcome distribution is roughly −1R / +3R at
   ~25% win rate, so **SD ≈ 1.7R per trade**, and the standard error of the mean
   is 1.7/√n:

   | n | SE(mean R) | two-sided 95% needs | one-sided 95% needs |
   |---:|---:|---:|---:|
   | 100 | 0.17 | +0.34R | +0.28R |
   | 150 | 0.14 | +0.28R | +0.23R |
   | 250 | 0.11 | +0.21R | +0.18R |

   The obvious-looking bar (n≥100, two-sided CI clears zero) demands a measured
   **+0.34R — the exact size of the artifact we just debunked.** It can only ever
   return "no edge" for a genuinely real-but-modest signal: a true +0.15R edge
   fails it at n=100 even if it exists. That is not a reason to lower the bar; it
   is a reason to choose it *knowingly*.

   **Registered criterion (proposed):** mean R **net of measured bid/ask**, on
   **resolved dual-confluence trades**, **n ≥ 150**, **one-sided 95% bootstrap CI
   lower bound > 0** (bar ≈ +0.23R). One-sided is defensible — we only care
   whether it clears zero, not by how much it might be below.

   **Power caveat — weight this heavily.** At 0.68 dual-confluence/week, n=150 is
   ~4.25 years of *holdout* calendar time. OANDA's ~15–20yr daily history can
   *just* fund that for the majors, but a 70/30 split then leaves train lopsided,
   and **forward paper to n=150 is ~4+ years.** Daily granularity is therefore
   borderline-underpowered by construction. If the obtainable sample can't
   distinguish a real modest edge from noise, the finding is **"this strategy
   cannot prove itself at daily granularity"** (outcome 4) — and *that*, not
   novelty, is the real argument for intraday: **more trades is the only route to
   statistical power.**
3. **Fit only on TRAIN.** Every choice — impulse threshold, retest window,
   `FX_MIN_SCORE`, any new setup — is selected on train data only.
4. **Freeze.** Commit the frozen parameters.
5. **Evaluate ONCE on the holdout.** Report that single number. **No iterating
   after seeing it.** If you tweak anything post-holdout, the holdout is burned —
   treat it as contaminated and the honest next step is fresh out-of-sample data
   (forward paper), not a second holdout pass.
6. **Forward paper is the real test.** A holdout is still in-sample-adjacent (it
   existed when you chose the method). The un-fakeable test is **forward** paper
   trading with parameters frozen, judge off, over enough weeks to reach the
   pre-registered n. OANDA practice is built for this.

**Reject-on-sight signatures** (write these on the wall): expectancy that rises
as n falls; a result that only appears at one exact parameter value; any number
you found by trying more than a handful of configurations; **and — the one that
will be most tempting at Gate 2 — the criterion (metric, n, or bar) being revised
after a result is seen.** Preventing that last move is the entire reason this
document is written before the data exists. The wider-o2c-window **+0.44R** result
from the last pass is the first trap — biggest number on the page, still built on
the degenerate open, not to be chased.

## 2. What the adapter is (technically)

A new `DataSource` implementation behind the **existing interface**
(`v2/datasource.py::DataSource`) — the strategy layer does not change.

- **OANDA v20 REST, practice environment** (`api-fxpractice.oanda.com`), Bearer
  token auth. **Data only** — candles/pricing. **No order placement** in this
  phase; paper simulation stays in our own ledger exactly as today. (Practice
  order execution is a separate, later, explicitly-gated step, if ever.)
- **Real bid/ask.** OANDA candles can be requested at bid, ask, and mid
  (`price=BAM`). This is the whole point: **drop the assumed per-pair spread
  table** and use the actual quoted spread for entry, R:R, and cost. Confirm exact
  request params (`granularity`, `price`, `count`/`from`/`to`, `complete` flag,
  per-candle `bid/ask/mid` OHLC + `volume`) against the current v20 docs — do not
  hardcode from memory.
- **Real opens + real intraday.** Daily candles with true opens (the c2c-vs-o2c
  question **dissolves** — you measure real moves). Intraday granularities (H1,
  M1) give honest intrabar resolution to replace the yfinance intraday path.
- **History depth.** v20 serves **max 5,000 candles per request**, paginated via
  `from`/`to`; practice accounts get the **same history as live**. Daily for the
  majors goes back ~15–20 years, so D1 across the basket is *not* data-constrained
  (well beyond yfinance) — a paginating fetch loop is all that's needed. Intraday
  (H1/M1) is where pagination volume actually matters. Confirm the per-pair depth
  empirically in Phase A rather than trusting these numbers.
- **Instrument mapping:** OANDA uses `EUR_USD`-style symbols; add a mapping
  alongside the existing basket. The `DataSource` methods (`symbols`,
  `fetch_daily`, `resolution_bars`, `pip_size`, `spread_pips`) map cleanly; make
  `spread_pips` return the **measured** spread rather than the assumed constant.

### Reused unchanged
The detectors, levels, `store.walk_trade` resolution, the replay/calibration
harness, and the honest-ledger machinery all work as-is against a new source —
they were built source-agnostic for this reason. The **only** additions are the
adapter and the train/holdout split plumbing.

### Config / secrets
`OANDA_API_TOKEN` and `OANDA_ACCOUNT_ID` from env (never committed, never live-
trading scoped); `BOT_MARKET=fx_oanda` (or a source flag) to select it; a
`TRAIN_HOLDOUT_BOUNDARY` date constant. Keep the yfinance FX source too, for
comparison.

## 3. Phased plan, with gates

- **Phase A — adapter + split, no tuning.** Implement the data adapter (candles,
  real bid/ask) and the train/holdout split. Run **one** honest baseline on
  **TRAIN only**, at the current frozen parameters, net of real spread. *Gate 1:*
  if even the train baseline is not non-negative net of real cost, **stop** — the
  clean data has already answered the question.
- **Phase B — minimal, pre-registered fit on TRAIN.** A **small, written-down**
  parameter set only (not an open-ended sweep). No holdout access.
- **Phase C — evaluate once on HOLDOUT.** Frozen params. Single number. Report it
  as-is against the pre-registered criterion. *Gate 2:* the three outcomes in §0.
- **Phase D — forward paper (only if C survives).** Frozen params, judge off, run
  the existing paper harness on the OANDA source until n reaches the registered
  threshold. This is the decision-grade evidence.

## 4. Non-goals / explicit guards

- Will **not** aim to reproduce or restore +0.35R (it was artifact).
- Will **not** chase the +0.44R wider-o2c result (artifact, best-of-N).
- Will **not** add intraday knobs (session filters, M1 entries, new setups)
  without each one living inside the same train/holdout discipline.
- Will **not** touch `FX_MIN_SCORE` or enable the Claude judge as part of this.
- **No live orders.** Practice account is for data (and later, at most, simulated
  practice fills behind an explicit gate).

## 5. Open questions (for you, before Phase A)

1. ~~Provision an OANDA practice account + API token?~~ **Resolved:** signup is
   free and near-instant; the token comes from the account-management page. No
   decision needed — just create it and put the token in env.
2. ~~How much history?~~ **Resolved (confirm empirically in Phase A):** v20 max
   5,000 candles/request, paginated; practice = live history; D1 for the majors
   ~15–20 years — not data-constrained. Caveat: reaching **n ≥ 150** dual-
   confluence trades in a *locked holdout* still costs ~4+ years of holdout
   calendar time at 0.68/week, which a 70/30 split of even 15–20 years funds only
   awkwardly. This is the power tension in §1, not a data-availability problem.
3. **Confirm the pre-registered acceptance criterion** (§1.2): mean R net of
   measured bid/ask, resolved dual-confluence trades, **n ≥ 150**, one-sided 95%
   bootstrap CI lower bound > 0. Agree it — or amend it — **now, in writing,
   before any fit.** After this line is committed it does not move (see the
   reject-on-sight list). If you'd rather trade the daily-granularity power
   problem for more trades, say so now and Phase A targets intraday from the
   start.

---

**One line to keep the whole thing honest:** OANDA changes the *data*, not the
*discipline*. Better data with the same best-of-N habits will just manufacture a
prettier fake. The split-and-evaluate-once protocol in §1 is the actual
deliverable; the adapter is the easy part.
