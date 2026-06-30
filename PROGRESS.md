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
