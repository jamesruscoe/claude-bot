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
| **Real edge** (positive net of real spread, meaningful n) | Proceed carefully — small size, long forward paper test. |
| **Marginal** (near zero, within noise) | Tiny size, long forward test, or shelve. Do **not** talk yourself into it. |
| **No edge** | Stop. Time spent, **no money lost** — a win relative to the alternative. |

If you are not willing to accept outcome 3 before looking, do not run the test.

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
2. **Pre-register the acceptance criterion** before fitting anything — the exact
   metric, the minimum sample (e.g. **n ≥ 100 resolved trades**), and the bar
   (e.g. positive expectancy **net of real bid/ask** with the CI not straddling
   zero). Written down, dated, in this repo, before results exist.
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
you found by trying more than a handful of configurations. The wider-o2c-window
**+0.44R** result from the last pass is precisely this trap — it is the biggest
number on the page, it is still built on the degenerate open, and it must not be
chased.

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

1. Provision an OANDA practice account + API token? (free.)
2. How much history does OANDA practice serve per instrument/granularity? (sets
   whether the 70/30 split has enough n for the registered threshold.)
3. Agree the **pre-registered acceptance criterion** now: metric, minimum n, and
   the exact bar — in writing, before any fit.

---

**One line to keep the whole thing honest:** OANDA changes the *data*, not the
*discipline*. Better data with the same best-of-N habits will just manufacture a
prettier fake. The split-and-evaluate-once protocol in §1 is the actual
deliverable; the adapter is the easy part.
