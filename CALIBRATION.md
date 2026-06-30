# CALIBRATION — fx

Frequency vs expectancy from the walk-forward replay (raw, unsized R; live detectors + honest intrabar resolution). The score threshold is the only frequency lever the detector exposes (scores are 0/50/100).

## Threshold curve (whole basket)

| Threshold | Resolved | Resolved/week | Win rate | Avg R (expectancy) | Verdict |
|-----------|---------:|--------------:|---------:|-------------------:|---------|
| ≥50 (all) | 404 | 3.07 | 18% | +0.05R | positive |
| =100 (dual) | 89 | 0.68 | 25% | +0.35R | positive |

## Proposed threshold

- **FX_MIN_SCORE = 100** — highest robust expectancy is at =100 (+0.35R, ~0.7/week). ≥50 trades ~3.1/week but only +0.05R (marginal, ~breakeven WR) — frequency without a clear edge, so not chosen. ~1/week at positive expectancy is approachable at =100; below 1/week is acceptable rather than loosening into noise.
- Currently set in config: **`FX_MIN_SCORE = 85`** (REVIEW marker removed — see Threshold review below).

## Threshold review — re-slice + 1.5x spread robustness

The detector emits **discrete scores {0, 50, 100}** (one setup = 50, both agree = 100). So any
threshold in (50, 100] selects exactly the **dual-confluence (score==100)** set, and there is **no
85–99 population** — that band is empty, not negative.

Re-sliced from the same walk-forward (no live run), raw unsized R:

| Threshold | 1.0x spread | 1.5x spread |
|-----------|-------------|-------------|
| ≥50 (all) | n=404, 18% WR, **+0.05R**, +20.4R | n=404, 17% WR, **+0.03R**, +12.9R |
| ≥85 = ≥90 = ≥100 (dual) | n=89, 25% WR, **+0.35R**, +31.3R | n=89, 25% WR, **+0.35R**, +30.9R |
| band 85–99 (isolated) | **n=0** (empty) | **n=0** (empty) |

**Decision:** ≥85 is clearly positive on a meaningful sample (n=89) **and survives the 1.5x spread
test essentially unchanged (+0.35R)** — the edge does not sit on a thin spread assumption. The
85–99 band is empty (not negative). Both conditions to lower the gate are met, so **FX_MIN_SCORE is
set to 85** and the `# REVIEW` marker is removed. Caveat: with discrete scoring, **85 ≡ 100 today** —
it admits the same dual-confluence trades; it would only differ if the detector is recalibrated to
emit intermediate scores. ≥50 (+0.05R, degrading to +0.03R at 1.5x) remains deliberately excluded.
Still **paper-only** until the live ledger confirms.


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
