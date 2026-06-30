# PROGRESS — FX + Claude API refactor

Tracks each phase: what changed, what the numbers say, what needs review.
Equities remains the default/safe path (`BOT_MARKET=equities`); FX is opt-in
(`BOT_MARKET=fx`). Paper only — no live execution anywhere.

---

## Phase 1 — Data + ledger foundation ✅

**What changed**
- **Data interface** (`v2/datasource.py`): one `DataSource` protocol; `EquitiesSource`
  wraps the existing Massive `market_data` unchanged, `FXSource` is a new yfinance adapter
  (daily 3y + intraday 1m/1h) with **retry, on-disk cache (TTL), and a stale/empty guard** —
  an empty or old pull makes the caller skip rather than act on bad data. Factory
  `get_data_source()` picks by `BOT_MARKET` (defaults to equities). OANDA can slot in behind
  the same protocol later.
- **Honest resolution** (`v2/store.py::walk_trade`): replaced the close-only resolver (the
  audit's master bug) with intrabar walking over OHLC bars — a bar whose HIGH/LOW pierces a
  level is a real win/loss, no longer silently expired at ~0R. **SL-first tie-break** when a
  single bar spans both stop and target. Live FX resolves on intraday bars; equities/replay on
  daily. Tested across win/loss/tie/breakeven/expiry/short.
- **Sized R** (audit HIGH bug): `open_trade` now records `size` + `size_mult`; resolution writes
  `pnl_r = raw_r * size_mult` (and keeps `raw_r` for diagnostics). Sizing is no longer cosmetic.
- **Pip/spread risk math** (`v2/levels.py::compute_levels_fx`): stops/TPs in pip terms; entry
  worsened by a **fixed conservative per-pair spread** (mid-price feed, so spread is assumed —
  table in `v2/config.py`); R:R computed post-spread; lots from fixed-fractional risk
  (0.5%/equity). Lots are bookkeeping — R is independent of lot size.
- **Rejection logging**: every rejected candidate is persisted to a new `rejections` table with
  a reason (`no_setup`, `regime_blocked`, `conflicting_setups`, `levels_rejected_wide_stop`,
  `judge_skip`, `stale_feed`, …). "Why did nothing fire" is now answerable from the ledger.
- **Audit dead-code cleanup**: `--resolve-only` is now wired (`pipeline.resolve_only`); the
  inert live-price staleness guard and the never-called `fetch_live_price` are removed.
- **Replay harness** (`v2/replay.py`, `python run.py --replay`): walk-forward over history using
  the SAME detectors/levels/resolution (no flattering simulator), writes `BASELINE.md`.
- **Tests**: 18 unittest cases (stdlib, no new deps) — resolution semantics, sized R, FX levels,
  rejection reasons, datasource factory/parsing. `python -m unittest discover -s tests`.

**What the numbers say (`BASELINE.md`, FX basket, ~3y daily)**
- Raw daily SMC on FX is **negative expectancy**: score≥50 → ~14% win rate, **−0.20R avg**,
  −70R total over 355 resolved; dual-confluence (=100) essentially never fires (1 in 3y).
- The tight 0.5·ATR stop gets run over far more often than the 2R target prints (LOSS 249 vs
  WIN 42). This is the honest read: **the strategy as-ported has no edge on FX daily.** It is
  evidence that Phase 2 must calibrate the detector to FX (pip/ATR ranges, sessions) — not a
  reason to loosen thresholds.

**Needs review**
- Nothing blocking. Note for later: the negative baseline strongly suggests the basket may not
  reach "1 trade/week at positive expectancy" (Phase 3 anticipates this). Confirmed honest
  measurement is in place before any tuning.

**Exit criterion met:** honest ledger (sized R, intrabar-resolved) + `BASELINE.md` exist; tests
and selftest green. Equities path unchanged.

---

## Phase 2 — FX-native strategy + filters ✅

**What changed** (all FX-only, gated on `FX_ENABLED`; equities untouched)
- **FX detector calibration**: OB impulse threshold is now configurable and defaults to **0.8%**
  for FX (the equities 3% almost never fires on sub-1% FX daily ranges). Threaded through
  `build_candidate(impulse_threshold=…)` and the replay so Phase 3 calibrates the tuned detector.
- **Session filter** (`v2/fx_filters.py::session_ok`): `off` (default, safe) | `overlap`
  (London/NY 12–16 UTC) | `skip_asia` (block thin Asia hours for non-JPY pairs). Config-driven.
- **Scheduled-news avoidance** (`v2/news_calendar.py`): ForexFactory weekly JSON, cached 1h,
  blocks opening within ±45 min of a high-impact event for either of the pair's currencies.
  **Fails open** — a feed outage logs + allows (never silently freezes the bot) and the attempt
  is recorded. Parsing is isolated from fetching and unit-tested offline.
- **Correlation-aware exposure cap** (`v2/fx_filters.py::correlation_cap_ok`): caps net same-
  direction exposure per currency at `FX_MAX_PER_CCY` (default **2**). Long EURUSD + long GBPUSD
  count as two USD-shorts toward the cap, so one macro view can't open as six tickets. A cap only
  blocks, so it's on by default (conservative direction).
- **Regime filter kept** (audit-confirmed). Period exposed via `FX_REGIME_MA_PERIOD` (50) for
  re-fitting; not removed.
- All three open-time gates wired into the pipeline's open branch and recorded as rejection
  reasons (`fx_session` / `fx_news` / `fx_correlation`).
- **Tests**: +9 cases (session modes, correlation cap incl. offsetting exposure, news parse /
  blackout window / fail-open). 27 total, all green.

**Needs review**
- The 0.8% FX OB impulse is a sensible starting calibration, not a tuned value — Phase 3's
  `CALIBRATION.md` shows the frequency/expectancy curve and proposes the score threshold.

**Exit criterion met:** filters implemented, config-driven, unit-tested, committed.

---

## Phase 3 — Calibrate frequency to expectancy ✅ (⚠ THRESHOLD AWAITS YOUR REVIEW)

**What changed**
- **Graduated probationary sizing** (`v2/brain.py`): replaced the hard 5-trade cold-start skip
  (the audit deadlock) with a ramp — cold (0–1 decided)=quarter, thin (2–4)=quarter/half,
  meaningful(≥5)=win-rate-driven (full/half/quarter). A structurally-valid candidate is **never
  skipped for lack of history**; only a *proven-bad* meaningful record is hard-skipped. Applies to
  both paths (it's the audit fix). +5 brain tests.
- **`CALIBRATION.md`** via `python run.py --calibrate` (FX). Frequency-vs-expectancy across the
  only lever the detector exposes (scores 0/50/100), with a corrected **basket** frequency
  (calendar weeks, not summed symbol-weeks).
- **Proposed threshold written to config**: `FX_MIN_SCORE` with `# REVIEW: proposed by
  calibration`, enforced on the FX open path.

**What the numbers say** (FX-calibrated detector, ~2.5y daily)
| Threshold | Resolved | /week | Win rate | Avg R | Verdict |
|-----------|---------:|------:|---------:|------:|---------|
| ≥50 (all) | 404 | 3.07 | 18% | +0.05R | marginal (~noise) |
| =100 (dual) | 89 | 0.68 | 25% | **+0.35R** | positive, meaningful |

- Calibrating the OB impulse to FX (Phase 2) turned dual-confluence from "never fires" (1 in the
  Phase-1 baseline) into **89 trades at +0.35R** — a real, measured edge at ~0.7/week.
- **Proposed `FX_MIN_SCORE = 100`** (dual-confluence only). ≥50 trades ~3/week but at +0.05R
  (~breakeven WR) — frequency without a clear edge, deliberately NOT chosen. Per the brief, picked
  the highest-robust-expectancy threshold even though it's just under 1/week.

**⚠ Needs your review (GATE — this is your decision):**
- **Confirm or change `FX_MIN_SCORE`.** Default is the conservative 100. Trade-off: 100 ≈
  0.7/week at +0.35R; 50 ≈ 3/week at +0.05R (marginal). I do **not** treat my choice as final.
- These are raw, unsized, replay numbers on a delayed mid-price feed; live paper results may
  differ. Recommend running paper (Phase 5) before any scale-up or going live.

**Exit criterion met:** `CALIBRATION.md` + conservatively-defaulted, flagged threshold; committed.

---

## Phase 4 — Claude API judge ✅ (off by default)

**What changed**
- **Haiku judge** (`claude-haiku-4-5`, verified live with your `ANTHROPIC_API_KEY`). Only candidates
  reach it — never per-bar, never the universe. Input includes the setup, session, news proximity,
  and correlation with the open book (`pipeline._attach_fx_context`). Output is **strict JSON**
  (`verdict` / `confidence` / `size_multiplier` / `reason`), parsed defensively
  (`llm.parse_verdict`); on malformed output or any API error it **falls back to the deterministic
  verdict** and logs it — a flaky model can never break a scan. Still gated behind `BOT_LLM=1`
  (+ `BOT_LLM_PROVIDER=anthropic`); **off by default**.
- **Cost log** (`llm.log_cost`): every call appends `{model, tokens, est_usd, batch}` to
  `state/llm_cost.jsonl` and logs a line. Pricing table in config (Haiku $1/$5 per MTok).
- **Offline batch second-opinion** (`v2/batch_judge.py`, `run.py --batch-second-opinion`): nightly
  job re-judges the day's candidates via the **Message Batches API** (50% off), records each verdict
  in a `second_opinions` table with whether it **agreed** with the live deterministic decision, and
  reports an agreement rate. Purely **observational** — never affects live decisions. Verified the
  live single-call judge; the batch path is wired and unit-covered (network not exercised in CI).
- **Tests**: +9 (verdict parsing, size-multiplier buckets, JSON-fence stripping, cost math incl.
  batch discount). 41 total, all green.

**Needs review**
- Judge stays **off**. Turn it on only once you want to compare Claude against the free brain —
  run `--batch-second-opinion` for a while first and read the agreement rate before flipping
  `BOT_LLM=1` live.

**Exit criterion met:** judge integrated behind a flag (off by default), JSON parsing + fallback
tested, committed.

---

## Phase 5 — Run harness (paper only) ✅

**What changed**
- **Daily cron entry point**: `.github/workflows/scan-fx.yml` runs the FX scan daily (21:30 UTC),
  persisting the honest ledger to a dedicated **`state-fx`** branch (mirrors the equities v2
  workflow). Claude judge **off** in CI; deterministic brain runs free.
- **Simulated fills only** — entries use the yfinance **mid + assumed per-pair spread**
  (`levels.compute_levels_fx`); resolution is intrabar SL-first (`store.walk_trade`). **No live
  execution path exists anywhere** in the codebase.
- **Daily report** (`v2/report.py`, `run.py --report`, also written automatically each scan to
  `state/daily_report.md`): candidates + verdicts, rejections-by-reason, paper trades opened/closed
  this run, running **sized** expectancy, and the Claude↔deterministic agreement rate when present.
- **OANDA practice** left as a documented TODO (ARCHITECTURE.md → *TODO — OANDA practice*) — a new
  `DataSource` impl for real bid/ask; strategy layer unchanged.
- **Tests**: +2 (report rendering incl. opened/blocked/skipped). **43 total, all green.**

**Verified live:** `BOT_MARKET=fx python run.py --force` end-to-end — found a candidate, the
graduated judge took it at quarter size, the calibrated `FX_MIN_SCORE=100` correctly held it back
(paper-conservative), and the daily report rendered.

**Exit criterion met:** cron runs end-to-end in paper mode and produces a daily report.

---

## Final deliverables & status

- **Working FX paper bot**, equities path still switchable (`BOT_MARKET`, default equities).
- **`BASELINE.md`** (raw setup edge), **`CALIBRATION.md`** (frequency vs expectancy + proposed
  threshold), **`PROGRESS.md`** (this file).
- New code behind flags defaulting safe/off; **43 tests passing**; no hardcoded secrets
  (`ANTHROPIC_API_KEY` from env); **no live execution path**.

### Threshold resolved (was the Phase 3 GATE)
- **`FX_MIN_SCORE = 85`**, `# REVIEW` marker removed. Robustness review (re-slice + 1.5x spread, see
  `CALIBRATION.md`): dual-confluence is +0.35R over n=89 and **holds at +0.35R under 1.5x spread**.
  Because the detector scores discretely {50,100}, **85 ≡ 100 operationally today** (the 85–99 band is
  empty) — it only diverges if the detector is recalibrated to emit intermediate scores. Still paper-only.

### ⚠ Awaiting your review
1. **Whether to turn the Claude judge on.** Off by default. Recommend running `--batch-second-opinion`
   for a while and reading the agreement rate before flipping `BOT_LLM=1` live.
3. **Assumptions made** (proceeding on the brief, since `REFACTOR_PLAN.md` was absent): FX OB impulse
   calibrated to 0.8%; assumed per-pair spreads (config table); fixed-fractional risk 0.5%; honest
   resolution applied to both paths (it's the audit fix); graduated sizing applied to the shared
   deterministic brain (so equities decisions changed too). Flag any you'd like revisited.
