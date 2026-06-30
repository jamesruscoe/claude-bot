# CALIBRATION — fx

Frequency vs expectancy from the walk-forward replay (raw, unsized R; live detectors + honest intrabar resolution). The score threshold is the only frequency lever the detector exposes (scores are 0/50/100).

## Threshold curve (whole basket)

| Threshold | Resolved | Resolved/week | Win rate | Avg R (expectancy) | Verdict |
|-----------|---------:|--------------:|---------:|-------------------:|---------|
| ≥50 (all) | 404 | 3.07 | 18% | +0.05R | positive |
| =100 (dual) | 89 | 0.68 | 25% | +0.35R | positive |

## Proposed threshold

- **FX_MIN_SCORE = 100** — highest robust expectancy is at =100 (+0.35R, ~0.7/week). ≥50 trades ~3.1/week but only +0.05R (marginal, ~breakeven WR) — frequency without a clear edge, so not chosen. ~1/week at positive expectancy is approachable at =100; below 1/week is acceptable rather than loosening into noise.
- Currently set in config: `FX_MIN_SCORE = 100` (marked `# REVIEW: proposed by calibration`).


## Per-pair expectancy (score ≥ 50)

| Symbol | Resolved | Win rate | Avg R |
|--------|---------:|---------:|------:|
| EURUSD=X | 45 | 14% | +0.12R |
| GBPUSD=X | 45 | 18% | +0.07R |
| USDJPY=X | 49 | 19% | +0.21R |
| USDCHF=X | 50 | 26% | +0.30R |
| AUDUSD=X | 51 | 13% | -0.09R |
| USDCAD=X | 27 | 18% | +0.03R |
| NZDUSD=X | 51 | 19% | -0.05R |
| EURGBP=X | 37 | 7% | -0.32R |
| EURJPY=X | 49 | 21% | +0.10R |

> **This threshold is a PROPOSAL for your review, not a final decision.** More signals is not the goal — positive measured expectancy is. The graduated probationary sizing (Phase 3) lets the bot accrue a real record at tiny size before any scale-up.
