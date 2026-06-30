# claude-bot — Weakness/Risk Audit + "Why it under-signals" investigation

Investigation only. No code was changed. Line references are to the v2 system
(`run.py` + `v2/*`) and the three reused root modules (`config.py`,
`market_data.py`, `smc_detector.py`).

---

## 1. Pipeline overview

**Language / entry point.** Python. One scan = `python run.py` → `v2/pipeline.py::run_scan`
(async). `--selftest` runs an offline synthetic loop; `--force` bypasses the market gate;
`--llm-test` pings the optional LLM. The deterministic brain is the default and is free.

**End-to-end flow** (one scan):

```
run.py
  └─ pipeline.run_scan(force)
       1. calendar_gate.is_trading_day()         ── weekend/holiday? skip whole scan
       2. for each of 10 symbols (sequential):
            market_data.fetch_daily()             ── Massive daily bars (USOIL→yfinance)
       3. per symbol with bars:
            calendar_gate.bars_are_fresh(bar[-1]) ── >4 calendar days old? skip symbol
            signals.build_candidate()
               ├─ smc_detector.score_setups()     ── OB-retest + BOS-retest, 50MA regime
               ├─ smc_detector.invalidate_by_price ── (fed the daily close — see B6)
               ├─ score < 50 or no direction? → None
               ├─ levels.compute_levels()         ── 0.5·ATR stop, 2R/3R TP, midpoint fill
               └─ risk > 8% of price? → None       (R:R floor)
            journal.retrieve_for()                ── symbol track record + similar trades + lessons
            brain.judge()                         ── take/skip + size  (deterministic; LLM optional)
            store.record_signal() + record_decision()
            if take AND _should_open():           ── dedup: not already in this symbol+dir
                 store.open_trade()               ── fill = zone midpoint
       4. store.resolve_open_trades(prices)       ── adjudicate opens vs TODAY'S CLOSE only
       5. brain.reflect_on_closed()               ── journal markdown + distil lessons
       6. _emit()                                 ── last_scan.json + TAKE-TRADE marker for CI email
```

**Stores.** SQLite ledger (`state/ledger.db`: `signals`, `decisions`, `trades`, `lessons`)
is the source of truth; markdown journal/lessons under `state/` are the narrative the judge
reads. Both persist to the `state` branch in CI.

### External data sources

| Source | Used for | Free-tier limit | Reliability |
|--------|----------|-----------------|-------------|
| **Massive / Polygon** (`api.polygon.io`) | Daily candles (9/10 symbols) + reference news | **5 req/min**, EOD daily only (intraday returns empty on Stocks Basic) | OK for EOD; key rides in query string (logging silenced in `run.py`) |
| **yfinance / Yahoo** | Daily for USOIL (`CL=F`); live price + 1H (defined, mostly unused in v2) | No documented cap; throttles bursts (0.5s spacing enforced) | Unofficial scrape; can fail silently |

News (`fetch_news`, `apply_news_sentiment`) exists in the codebase but **is not wired into the
v2 pipeline** — there is no active news veto in v2.

---

## 2. Why it under-signals — ranked, with evidence

I replayed the **actual detectors** (`smc_detector`) over the last **261 trading days** for all
10 universe symbols via yfinance daily bars (2,610 symbol-days). Counts are the number of days
each condition was true for the "current bar":

| Symbol | days | OB fires | BOS fires | both | agree→100 | conflict→0 | regime-blocked | **score 50** | **score 100** |
|--------|-----:|-----:|------:|----:|------:|------:|------:|----:|----:|
| ARM | 261 | 71 | 31 | 10 | 9 | 1 | 26 | 56 | 9 |
| NVDA | 261 | 54 | 38 | 10 | 8 | 2 | 32 | 44 | 4 |
| TSLA | 261 | 79 | 35 | 11 | 10 | 1 | 44 | 52 | 6 |
| USOIL | 261 | 65 | 40 | 11 | 10 | 1 | 33 | 55 | 5 |
| SMCI | 261 | 100 | 27 | 9 | 8 | 1 | 59 | 53 | 5 |
| APLD | 261 | 143 | 29 | 10 | 10 | 0 | 90 | 66 | 6 |
| AMZN | 261 | 27 | 36 | 7 | 7 | 0 | 14 | 37 | 5 |
| NFLX | 261 | 27 | 34 | 5 | 5 | 0 | 19 | 33 | 4 |
| AMD | 261 | 76 | 27 | 11 | 10 | 1 | 43 | 43 | 5 |
| COIN | 261 | 107 | 29 | 9 | 9 | 1 | 57 | 65 | 5 |
| **TOTAL** | **2610** | 749 | 326 | 93 | 86 | 7 | **417** | **504** | **54** |

> Across 2,610 symbol-days: **dual-confluence (score 100) = 54 (2.1%)**, single-setup (score 50)
> = 504 (19.3%). Per *calendar scan day* across the whole 10-symbol universe that's ~**0.21
> dual-confluence** and ~1.9 single-setup candidates.

### Culprit #1 — Single-setup signals are hard-skipped at cold start (HIGH)
`brain._judge_deterministic` ([brain.py:78-83](v2/brain.py#L78-L83)): a score-50 candidate is
**taken only if** the symbol is `meaningful` (≥5 *decided* trades) **and** win rate ≥ 50%.
`meaningful` = `decided >= 5` where `decided = wins + losses` ([store.py:351-366](v2/store.py#L351-L366)).
The ledger starts empty and the only way to add a *decided* trade is to take one — but single-setup
trades are skipped until a track record exists. **Deadlock.**

Consequence: at cold start only **score-100** candidates can ever open. From the table that's **54
opportunities/year across all 10 symbols (~1/week)** — and that is *before* the levels R:R floor and
dedup trim it further. The 504 single-setup opportunities/year are all discarded. This is the single
biggest reason the bot barely signals.

### Culprit #2 — Daily detectors only fire on the *first* retest bar (HIGH)
Both detectors require the **current bar to be the first** to touch the zone/level, and that **no
prior bar** since the impulse/break touched it ([smc_detector.py:107-119](smc_detector.py#L107-L119)
for OB; [smc_detector.py:200-219](smc_detector.py#L200-L219) for BOS). On daily bars that is a
*one-day* firing window per impulse. Miss the day (or run on a holiday, or the bar isn't fresh) and
the setup is gone. This is structurally correct for "retest" semantics but caps candidates hard, and
it's why dual-confluence (both setups landing their one-day window on the *same* day) is so rare (93
co-occurrences/2,610, only 86 aligned).

### Culprit #3 — Close-only trade resolution starves the track record (HIGH, indirect)
`resolve_open_trades` is fed `prices = {symbol: bars[-1].c}` — the **daily close only**
([pipeline.py:73](v2/pipeline.py#L73), [pipeline.py:107](v2/pipeline.py#L107)). `_decide`
([store.py:252-284](v2/store.py#L252-L284)) therefore only triggers SL/TP1/TP2 when a **close**
breaches them; the day's high/low are ignored even though full OHLC bars are available. A trade that
pierces TP2 or the stop intraday but closes back inside is recorded as *still open* and eventually
tagged **EXPIRED** at ~0R after 10 days. EXPIRED/BREAKEVEN are **excluded** from `decided`
([store.py:362-365](v2/store.py#L362-L365)), so they never advance the `meaningful` counter that
Culprit #1 depends on. Net effect: wins and losses are systematically under-recorded → the gate in
#1 may *never* open in practice → permanent scarcity, and the bot's edge is left unmeasurable.

### Culprit #4 — 50MA regime filter (MEDIUM — mostly correct caution)
`regime_filter` zeroes any setup against the 50-day MA ([smc_detector.py:234-258](smc_detector.py#L234-L258)).
It blocked **417** otherwise-scoring setups in the replay (~14% of all symbol-days, and a large share
of raw OB/BOS fires — e.g. APLD 90, SMCI 59, COIN 57). This is the right kind of filter (don't fade the
trend), but it is a major volume reducer and worth being aware of. **Keep it.**

### Culprit #5 — Small universe + AND-style confluence definition (MEDIUM)
10 symbols ([config.py:38-49](config.py#L38-L49)). "Confluence" is defined as OB **and** BOS firing
the same day and agreeing — a strict AND. Combined with #2, dual-confluence is inherently a ~2%/day
event. Conflicts (OB vs BOS disagree) zero the score entirely (7 cases) rather than deferring to the
stronger/most-recent signal.

### Culprit #6 — `MAX_RISK_PCT` and R:R floor (LOW-MEDIUM)
`compute_levels` rejects any setup whose stop is >8% of price ([levels.py:71-72](v2/levels.py#L71-L72)).
On wide-range OB candles in volatile names (APLD/SMCI/COIN) the zone half-width + 0.5·ATR can exceed
8%, silently dropping the candidate. Reasonable, but it removes some otherwise-valid score-100s and is
not logged with a reason.

### Not currently suppressing anything
- **News veto**: not wired into v2 (no effect).
- **`DEDUP_WINDOW_HOURS` / `DEDUP_ZONE_PCT`** and `store.recent_signal_for_dedup`
  ([store.py:342-346](v2/store.py#L342-L346)): defined but **never called**. The only live dedup is
  "already in an open position" ([pipeline.py:127-136](v2/pipeline.py#L127-L136)), which is fine.
- **Time-window / cooldown / max-open-positions**: none in v2 (cooling-off was folded into the
  negative-record hard-skip — correct). There is **no max-open-positions cap** (see risk list).

---

## 3. Correctness & risk audit (ranked)

### HIGH
1. **Close-only resolution mislabels outcomes** — Culprit #3 above. Both a correctness issue (a real
   TP2 day recorded as "open"/EXPIRED) and the engine of the under-signal deadlock. Full OHLC is in
   hand; only the close is used.
2. **Position size is computed but never applied.** `brain.judge` returns `size` ∈
   none/quarter/half/full, `record_decision` stores it, but `open_trade`
   ([store.py:165-194](v2/store.py#L165-L194)) ignores it and `_pnl_r` is pure R. So sizing has **zero
   effect on recorded expectancy** — the "size up/down with the record" logic is cosmetic. Any future
   "this strategy makes money" conclusion drawn from `total_r` is unweighted by the sizing it claims.

### MEDIUM
3. **`--resolve-only` is dead.** Documented in `run.py` help ([run.py:6](run.py#L6), [run.py:39-40](run.py#L39-L40))
   and ARCHITECTURE/README, but `main()` never reads it — it always calls `run_scan(force=...)`
   ([run.py:64](run.py#L64)). The advertised "just adjudicate open trades" mode does nothing.
4. **Live-price staleness guard is effectively inert.** `signals.build_candidate` passes
   `live_price = bars[-1].c` (the close) ([pipeline.py:83](v2/pipeline.py#L83)); `fetch_live_price`
   exists in `market_data` but **is never called by v2**. So `invalidate_by_price`
   ([smc_detector.py:418-468](smc_detector.py#L418-L468)) compares the close against zones derived from
   the same bar series — the "intraday correction for EOD lag" described in
   [signals.py:68-70](v2/signals.py#L68-L70) largely doesn't happen. Not a signal *suppressor*, but the
   code's stated protection against stale retests is missing.
5. **No max-open-positions / correlation cap.** The universe is highly correlated (NVDA/AMD/SMCI/ARM/APLD
   are all AI-semis; COIN/TSLA high-beta). In a strong regime, dual-confluence can fire across several at
   once and the bot will open all of them — concentrated, correlated risk with no portfolio cap.
6. **`meaningful` vs `avg_r` denominator mismatch.** `meaningful`/`win_rate` count only WIN_TP2 + LOSS,
   but `avg_r`/`total_r` average **all** closed including BREAKEVEN/EXPIRED ([store.py:351-366](v2/store.py#L351-L366)).
   The negative-record hard-skip ([brain.py:68](v2/brain.py#L68)) mixes the two (`wr < 0.35 or avg_r < 0`),
   so a symbol with many ~0R expiries can be pushed negative on `avg_r` and hard-skipped on essentially
   no decided information.

### LOW
7. **Detector-level rejections aren't persisted.** Candidates that score 0 / fail levels never hit the
   `signals` table (only built candidates do), so you can't audit "why nothing fired" from the ledger —
   exactly the question this report had to answer by external replay.
8. **`bars_are_fresh` uses `timedelta.days`** ([calendar_gate.py:89](v2/calendar_gate.py#L89)) — integer
   truncation; combined with UTC bar timestamps this is coarse but within the 4-day tolerance, so benign.
9. **Silent swallows.** `_fetch` logs and returns `[]` on any exception ([pipeline.py:41-43](v2/pipeline.py#L41-L43));
   a whole-universe outage yields "no data — aborting" with no alert. Acceptable, but a totally empty scan
   should probably notify.
10. **No look-ahead bias found.** Detectors only read `bars[:current]`; ATR/SMA use trailing windows;
    resolution uses forward snapshots correctly. `_pnl_r` uses `original_sl` (not the trailed stop) — correct.
    The "let winners run" two-phase logic ([store.py:252-284](v2/store.py#L252-L284)) is sound in principle;
    its only flaw is the close-only feed (#1).

---

## 4. Minimal-diff fixes — each tagged with effect on **frequency** and **expectancy**

Ordered by value. "Freq" = effect on number of trades; "Exp" = effect on edge/measurement quality.

1. **Resolve against the bar's high/low, not just the close.**
   Pass `(high, low, close)` for the latest bar into `resolve_open_trades`; in `_decide` test the stop
   against the adverse extreme and TP against the favourable extreme (keep "stop checked first" ordering
   for the ambiguous case). ~15 lines, no schema change.
   • **Freq:** ↑↑ indirectly — trades resolve in days not weeks, `decided` grows, which *unlocks*
   single-setup signals (Culprit #1). • **Exp:** ↑ large — wins/losses are recorded honestly instead of
   dissolving into EXPIRED; this is what makes any expectancy number trustworthy. *Caveat:* a single daily
   bar still can't prove TP-before-stop on a day that touches both; the conservative ordering keeps the
   record from flattering itself.

2. **Apply `size` to recorded R, or drop the pretense.**
   Multiply `pnl_r` (or store a `size_mult`) by the decision's size in `open_trade`/resolution. ~5 lines.
   • **Freq:** none. • **Exp:** ↑ — `total_r` finally reflects the sizing strategy; thin-sample trades
   contribute less, proven symbols more, which is the whole point of the memory loop.

3. **Replace the single-setup *hard skip* with a *minimum size*, gated on a real risk budget.**
   Today score-50 with no record = skip. Instead: take score-50 at `quarter` size even with a thin sample,
   capped by a new max-open-positions / max-open-risk budget (see #4). ~6 lines in `brain.py:78-99`.
   • **Freq:** ↑↑ — unlocks the 504/year single-setup population (the deadlock breaker). • **Exp:**
   *neutral-to-slightly-down per trade* but ↑ in *information*: you cannot learn whether single-setups have
   an edge while refusing to ever take one at risk-controlled size. This is the intended "prove it on the
   free path" behaviour. **Do not pair this with also loosening the detector (#2/Culprit #2) — change one
   lever at a time.**

4. **Add a max-open-positions / max-open-correlated-risk cap.**
   New config + a count check in `_should_open`. ~8 lines.
   • **Freq:** ↓ slightly in hot regimes (by design). • **Exp:** ↑ — prevents the correlated-cluster blow-up
   that a naive frequency increase (#3) would otherwise enable. Ship #4 *with* #3.

5. **Wire `--resolve-only`** (thread the flag into `run_scan`, run steps 4-6 only). ~6 lines.
   • **Freq:** none. • **Exp:** ↑ operability — lets CI adjudicate opens after hours without re-scanning;
   complements #1 by resolving faster.

6. **Persist rejected candidates** (a lightweight `rejections` row, or reuse `signals` with a reason
   column for score-0/levels-fail). ~10 lines.
   • **Freq:** none. • **Exp:** ↑ — makes "why so few signals" answerable from the ledger; required to tune
   thresholds with data instead of replay.

7. **(Optional, measured) Feed a real live price** to `invalidate_by_price` via `fetch_live_price`, or
   delete the dead guard.
   • **Freq:** ↓ slightly (drops genuinely-stale retests). • **Exp:** ↑ — fewer fills into setups price has
   already left. Low priority; only matters once trading live intraday.

---

## 5. "Don't do this" — loosenings that buy trade count by selling edge

- **Don't drop `CANDIDATE_MIN_SCORE` below 50 / start taking score-0.** Score 0 means *no* structure or
  *conflicting* structure or *against-regime*. These are the lowest-quality states by construction.
- **Don't remove or invert the 50MA regime filter.** It blocked 417 setups in the replay; those are
  counter-trend retests — historically the worst bucket. Keeping it is correct caution.
- **Don't widen the OB/BOS retest window into a multi-bar "anywhere near the zone" trigger** *and*
  unlock single-setups at the same time. Each separately is a real lever; together they multiply and you
  lose the ability to attribute the change. Widening the window also degrades the "first clean retest"
  premise the levels math relies on.
- **Don't raise `OB_IMPULSE_OVERRIDES` loosening across the board** to manufacture impulses on low-vol
  names — a 3% threshold tuned for large caps exists to avoid calling noise an impulse.
- **Don't keep "fixing" frequency by lowering thresholds before fixing resolution (#1).** Until outcomes
  are recorded honestly, more signals just means more unmeasured trades — you'd be scaling an unknown edge.
- **Don't turn on the LLM judge to "find more trades."** It's a reasoning layer over the same candidates,
  not a source of new ones, and it costs money. Prove the free path first (as designed).

### Where the current caution is correct and should stay
- The **negative-record hard-skip** (sub-35% / negative avg_r on a meaningful sample) — sound replacement
  for the old cooling-off blacklist.
- The **tight-stop + R-multiple targets + midpoint fill** in `levels.py` — directly fixes v1's greedy
  targets and flattering fills; do not revert to liquidity-pool targets.
- The **market-calendar + freshness gates** — cheap, correct, and the reason v1's Juneteenth incident
  can't recur.
- **Dual-confluence sizing up, thin-sample sizing down** — the right instinct; it just needs #2 (apply
  size) and #3 (don't *fully* skip singles) to actually function.

---

## TL;DR
The bot under-signals primarily because of a **cold-start deadlock**: single-setup signals (≈19%/day of
opportunities) are hard-skipped until a symbol has 5 *decided* trades, but **close-only resolution**
rarely produces decided trades (most expire at ~0R), so the gate never opens — leaving only rare
dual-confluence signals (≈2%/day, ~1/week across the universe). Fix resolution to use bar high/low (#1),
apply position size to recorded R (#2), and convert the single-setup hard-skip into a risk-capped minimum
size (#3 + #4). That breaks the deadlock *and* makes the resulting expectancy trustworthy — which matters
more than the raw count.
