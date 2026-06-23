# claude-bot v2 — architecture

A memory-driven SMC trading scanner. The rebuild fixes the three faults that
made v1 untrustworthy and adds a persistent memory loop so the bot learns from
its own outcomes instead of starting blank every run.

## Why a rebuild

Three concrete failures in v1, all visible in a single emailed scan:

1. **Amnesia.** Every state file (`paper_trades.json`, `cooling_off.json`,
   `fired_signals.json`, …) was `.gitignore`d and never restored, so every
   GitHub Actions run started on a blank filesystem. The "self-learning" paper
   trader never accumulated a single closed trade, dedup never worked across
   runs (→ the same SMCI signal emailed 3×), and the risk engine read from a
   ledger that reset every few hours.
2. **No market-hours awareness.** The cron ran Mon–Fri with no holiday
   calendar, so it scanned a stale Thursday close on Juneteenth and emailed a
   "TAKE TRADE" you couldn't act on.
3. **Greedy targets + flattering fills.** Targets came from liquidity pools
   2.5–3.5 ATR away that almost never printed; the paper trader then "filled"
   at the most favourable edge of the zone, quietly inflating the record.

## The shape

```
        ┌──────────────┐
        │ calendar_gate│  market open? bars fresh?   → skip cleanly if not
        └──────┬───────┘
               │
        ┌──────▼───────┐
        │   signals    │  salvaged v1 detectors (OB retest, BOS retest, ATR,
        │ (smc_detector│  swings, 50MA regime) PROPOSE a candidate + zone
        │  + levels)   │  levels.py → tight stop, 2R/3R targets, midpoint fill
        └──────┬───────┘
               │ candidate
        ┌──────▼───────┐        ┌─────────────┐
        │    journal   │◄───────│    store    │  SQLite ledger: signals,
        │  retrieve_for│        │  (truth)    │  decisions, trades, lessons
        └──────┬───────┘        └─────▲───────┘
               │ memory               │
        ┌──────▼───────┐              │
        │     brain    │  judge(candidate, memory) → take/skip + size + why
        │   (judge)    │  deterministic & FREE; Claude is an opt-in upgrade
        └──────┬───────┘              │
               │ decision             │ record + open (deduped)
        ┌──────▼───────┐              │
        │   pipeline   │──────────────┘  resolve open trades vs today's prices
        └──────┬───────┘
               │ newly-closed trades
        ┌──────▼───────┐
        │     brain    │  reflect_on_closed → write journal markdown + distil
        │  (reflect)   │  lessons the judge will read next time
        └──────────────┘
```

## Modules

| File | Responsibility |
|------|----------------|
| `run.py` | CLI entrypoint. `--selftest`, `--force`, `--resolve-only`. |
| `v2/config.py` | All tunables. Paths rooted at `STATE_DIR` (the `state` branch in CI). |
| `v2/calendar_gate.py` | NYSE open/holiday gate (`pandas-market-calendars`, hardcoded fallback) + bar-staleness check. |
| `v2/signals.py` | Runs the salvaged detectors, builds one candidate per symbol. |
| `v2/levels.py` | Tight ATR stop, 2R/3R targets, midpoint fill, wide-stop rejection. |
| `v2/store.py` | SQLite ledger — the hard source of truth. Ports v1's "let winners run" resolution. |
| `v2/journal.py` | Markdown journal (narrative memory) + relevance retrieval for the judge. |
| `v2/brain.py` | Judgment + reflection. Deterministic by default; Claude when enabled. |
| `v2/llm.py` | **Optional, off** Claude adapter. |
| `v2/pipeline.py` | Orchestrates one scan end to end. |

What's **reused** from v1: `market_data.py` (data fetch), `smc_detector.py`
(pure-math detectors), the universe/ticker config. What's **dropped**:
`analyser.py` (greedy targets), `paper_trader.py`/`memory.py`/`cooling_off.py`
(JSON files → SQLite), scan.py's scoring-as-decision.

## Memory — the core idea

Two complementary stores, both persisted to the `state` branch:

- **Ledger (`state/ledger.db`)** — structured truth. Every candidate, every
  judge decision, every trade and its outcome/R-multiple. Drives stats.
- **Journal (`state/journal/*.md`, `state/lessons/*.md`)** — narrative the
  reflection layer writes after each trade resolves, with frontmatter +
  `## What happened` / `## Lesson`. Browsable on GitHub; this is what the judge
  reads to reason about a new setup in light of similar past ones.

On each candidate, `journal.retrieve_for()` pulls the symbol's track record,
the most similar resolved trades (symbol + setup overlap + direction), and any
applicable lessons, and hands that to the judge.

## Judgment — free first, Claude later

`brain.judge()` is **deterministic and costs nothing**. It decides take/skip
and position size from the symbol's own track record: dual-confluence setups
start at half size; single-setup signals need a proven record; a meaningful
sub-35% win rate is a hard skip (this replaces v1's separate cooling-off
blacklist with one coherent rule); strong records size up.

LLM judgment (`v2/llm.py`) is a **dormant upgrade** behind `BOT_LLM=1`. It's
provider-pluggable:

- **`groq`** (default, **free**) — Llama 3.3 70B via Groq's OpenAI-compatible
  API. Fast, runs from GitHub Actions, no cost. Set `GROQ_API_KEY`
  (console.groq.com). Uses `httpx` + JSON mode; no extra dependency.
- **`anthropic`** (paid) — Claude, via `BOT_LLM_PROVIDER=anthropic` +
  `ANTHROPIC_API_KEY`. Needs the `anthropic` SDK.

Any LLM failure (network, bad JSON) silently falls back to the deterministic
judge, so a flaky model can never break a scan. Verify a key with
`python run.py --llm-test`. The point stands: **prove an edge on the free path
first**, and a free model (Groq) is smart enough for this task anyway — it's
structured summarization + a simple decision, not hard reasoning.

## Persistence in CI

The workflow (`.github/workflows/scan-v2.yml`) checks out the `state` branch
into a git worktree, points `BOT_STATE_DIR` at it, runs the scan, then commits
and pushes the worktree back. First run bootstraps an orphan `state` branch
automatically. `run.py` gates on the market calendar, so holiday/weekend runs
are cheap no-ops.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py --selftest     # offline end-to-end proof — no network, no API
python run.py --force        # real scan now (needs MASSIVE_API_KEY in .env)
```

State lands in `./state/` locally (gitignored), mirroring the CI layout.

## What's intentionally deferred

The dashboard, chart capture, watch loop, and the walk-forward backtest still
live in the v1 files and are **not yet ported** to v2. The highest-value next
step is porting the backtest onto the v2 levels so the new R-multiple targets
can be validated on history before the free judge runs live for a while.

## Honest caveat

SMC patterns may have no real edge — see the discussion that motivated this
rebuild. The architecture's job is to **measure that truthfully**: durable
memory, honest fills, hit-able targets, and a judge that defers to the actual
track record. Let the free version run, read the journal, and only scale (or
switch on Claude) once the ledger shows positive expectancy.
```
