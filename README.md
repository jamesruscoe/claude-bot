# claude-bot

A memory-driven SMC (Smart Money Concepts) trading scanner. Daily candles in,
honest trade briefs out — with a persistent ledger + journal so the bot learns
from its own resolved trades instead of starting blank every run.

The full design (modules, memory model, the judge, CI persistence) lives in
**[ARCHITECTURE.md](ARCHITECTURE.md)** — start there.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python run.py --selftest      # offline end-to-end proof — no network, no API key
python run.py --force         # real scan now (needs MASSIVE_API_KEY in .env)
python run.py --resolve-only  # only adjudicate open trades against fresh data
python run.py --llm-test      # verify the optional LLM judge key
```

Plain `python run.py` runs one scan gated on the NYSE calendar (a no-op on
holidays/weekends). State lands in `./state/` locally (gitignored), mirroring
the CI layout on the `state` branch.

## Judgment is free by default

`v2/brain.py` decides take/skip and position size deterministically from each
symbol's own track record — costs nothing. An LLM judge (Groq free, or paid
Anthropic) is a dormant opt-in behind `BOT_LLM=1`; any LLM failure silently
falls back to the free brain, so a flaky model can never break a scan. See
ARCHITECTURE.md → *Judgment*.

## Layout

| Path | What |
|------|------|
| `run.py` | CLI entrypoint |
| `v2/` | The system — gate, signals, levels, store, journal, brain, pipeline |
| `config.py`, `market_data.py`, `smc_detector.py` | Salvaged v1 internals reused by v2 (config, data fetch, pure-math detectors) |
| `.github/workflows/scan-v2.yml` | Scheduled scan + state persistence |
