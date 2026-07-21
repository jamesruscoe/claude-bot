"""Phase A — OANDA data-integrity pass + the single Gate 1 train baseline.

Order of operations (OANDA_ADAPTER_SCOPE.md §3, Phase A):

  1. Fetch real daily BAM candles for the basket.
  2. DATA-INTEGRITY PASS (before any baseline): date range/count per pair, the
     70/30 split the locked 2021-01-01 boundary actually produces, the real-open
     distribution (the thing Yahoo faked), a far-end spot check for synthetic
     history, and measured spread vs the assumed table.
  3. GATE 1: ONE honest baseline on TRAIN DATA ONLY (bars before the boundary),
     at the current frozen parameters, net of the MEASURED bid/ask spread. Report
     n, win rate, mean R, bootstrap CI — then STOP. No tuning, no holdout access.

The holdout is never fetched-past, sliced, or aggregated here. Everything is
train-only. Reuses the live detectors + honest resolution via replay.replay_symbol.
"""
from __future__ import annotations

import asyncio
import logging
import random
from statistics import mean, median
from typing import Any

from market_data import Bar
from v2 import config as cfg
from v2 import replay
from v2.oanda_source import OANDASource

log = logging.getLogger(__name__)

_BOOTSTRAP_ITERS = 10_000
_BOOTSTRAP_SEED = 20260721  # fixed so the CI is reproducible run-to-run


# --------------------------------------------------------------------------- #
# Train / holdout split (keyed on the LOCKED boundary)                         #
# --------------------------------------------------------------------------- #

def split_train_holdout(bars: list[Bar]) -> tuple[list[Bar], list[Bar]]:
    """TRAIN = bars strictly before the boundary; HOLDOUT = on/after it."""
    boundary = cfg.train_holdout_boundary()
    train = [b for b in bars if b.dt < boundary]
    holdout = [b for b in bars if b.dt >= boundary]
    return train, holdout


# --------------------------------------------------------------------------- #
# Bootstrap CI                                                                 #
# --------------------------------------------------------------------------- #

def bootstrap_mean_ci(values: list[float], *, iters: int = _BOOTSTRAP_ITERS
                      ) -> dict[str, float] | None:
    """Percentile bootstrap of the mean. Returns the point mean, the one-sided
    95% lower bound (5th pct — the registered test), and the two-sided 95%
    interval. Deterministic (fixed seed)."""
    n = len(values)
    if n < 2:
        return None
    rng = random.Random(_BOOTSTRAP_SEED)
    means: list[float] = []
    for _ in range(iters):
        s = 0.0
        for _ in range(n):
            s += values[rng.randrange(n)]
        means.append(s / n)
    means.sort()

    def pct(p: float) -> float:
        return means[min(len(means) - 1, int(p * len(means)))]

    return {
        "mean": mean(values),
        "one_sided_95_lower": pct(0.05),
        "two_sided_95_lower": pct(0.025),
        "two_sided_95_upper": pct(0.975),
    }


# --------------------------------------------------------------------------- #
# Data-integrity pass                                                          #
# --------------------------------------------------------------------------- #

def _open_move_stats(bars: list[Bar]) -> dict[str, float]:
    """(close-open)/open distribution. Yahoo's median was ~0.0001 (degenerate);
    real opens should be materially non-zero."""
    moves = [abs(b.c - b.o) / b.o for b in bars if b.o]
    moves.sort()
    return {
        "median_pct": round(median(moves) * 100, 4) if moves else 0.0,
        "mean_pct": round(mean(moves) * 100, 4) if moves else 0.0,
        "p90_pct": round(moves[int(0.9 * len(moves))] * 100, 4) if moves else 0.0,
        "zero_open_frac": round(sum(1 for m in moves if m == 0) / len(moves), 4) if moves else 0.0,
    }


def integrity_for_symbol(source: OANDASource, symbol: str, bars: list[Bar]) -> dict[str, Any]:
    train, holdout = split_train_holdout(bars)
    n = len(bars)
    train_frac = round(len(train) / n, 3) if n else 0.0
    far = bars[:20]  # oldest 20 bars — check for synthetic/flat history
    far_spread = source.measured_spread_stats(symbol, far)
    return {
        "symbol": symbol,
        "n_bars": n,
        "first": bars[0].dt.date().isoformat() if bars else None,
        "last": bars[-1].dt.date().isoformat() if bars else None,
        "n_train": len(train),
        "n_holdout": len(holdout),
        "train_frac": train_frac,
        "holdout_frac": round(len(holdout) / n, 3) if n else 0.0,
        "opens_all": _open_move_stats(bars),
        "opens_far_end": _open_move_stats(far),
        "spread_all": source.measured_spread_stats(symbol),
        "spread_train": source.measured_spread_stats(symbol, train),
        "spread_far_end": far_spread,
        "assumed_spread": cfg.fx_spread_pips(symbol),
    }


# --------------------------------------------------------------------------- #
# Gate 1 — train-only baseline at frozen params, net of measured spread        #
# --------------------------------------------------------------------------- #

def gate1_for_symbol(source: OANDASource, symbol: str, train_bars: list[Bar]) -> dict[str, Any]:
    """Walk TRAIN bars only, using the measured median spread over the train
    window as the (honest) cost. Returns the resolved trades."""
    stats = source.measured_spread_stats(symbol, train_bars)
    spread = stats["median"] if stats else cfg.fx_spread_pips(symbol)
    res = replay.replay_symbol(symbol, train_bars, spread_pips=spread,
                               max_lookback=cfg.FX_LIVE_DAILY_LOOKBACK)
    return {"symbol": symbol, "spread_used": spread, "trades": res["trades"],
            "rejections": res["rejections"]}


async def run_phase_a() -> dict[str, Any]:
    if not cfg.FX_OANDA:
        raise RuntimeError(
            "Phase A must run with BOT_MARKET=fx_oanda so FX risk math + the "
            "OANDA source are both selected.")
    source = OANDASource()
    integrity: list[dict[str, Any]] = []
    dual_trades: list[dict[str, Any]] = []   # score==100, TRAIN only, resolved-or-open
    per_symbol_gate1: list[dict[str, Any]] = []

    for symbol in source.symbols():
        bars = await source.fetch_daily(symbol)
        if len(bars) < 60:
            log.warning("phase A: %s only %d bars — skipping", symbol, len(bars))
            integrity.append({"symbol": symbol, "n_bars": len(bars), "skipped": True})
            continue
        integrity.append(integrity_for_symbol(source, symbol, bars))
        train, _holdout = split_train_holdout(bars)   # holdout discarded, untouched
        g = gate1_for_symbol(source, symbol, train)
        per_symbol_gate1.append(g)
        dual_trades += [{**t, "symbol": symbol}
                        for t in g["trades"] if t["score"] >= 100]

    await source.aclose()

    resolved = [t for t in dual_trades if t.get("outcome") is not None
                and t.get("raw_r") is not None]
    rs = [t["raw_r"] for t in resolved]
    wins = sum(1 for t in resolved if t["outcome"] in replay._WIN)
    losses = sum(1 for t in resolved if t["outcome"] in replay._LOSS)
    decided = wins + losses
    ci = bootstrap_mean_ci(rs) if len(rs) >= 2 else None

    gate1 = {
        "n_resolved": len(resolved),
        "n_still_open": len(dual_trades) - len(resolved),
        "wins": wins, "losses": losses,
        "win_rate": (wins / decided) if decided else None,
        "mean_r": mean(rs) if rs else None,
        "total_r": round(sum(rs), 2) if rs else 0.0,
        "ci": ci,
        "registered_n": cfg.FX_REGISTERED_MIN_N,
    }
    return {"integrity": integrity, "gate1": gate1,
            "per_symbol_gate1": per_symbol_gate1}


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #

def _fmt_r(x: float | None) -> str:
    return f"{x:+.3f}R" if x is not None else "n/a"


def _fmt_pct(x: float | None) -> str:
    return f"{x * 100:.0f}%" if x is not None else "n/a"


def write_report(data: dict[str, Any], path: str = "OANDA_PHASE_A.md") -> str:
    g = data["gate1"]
    integ = [i for i in data["integrity"] if not i.get("skipped")]
    lines: list[str] = []
    lines.append("# OANDA Phase A — data integrity + Gate 1 (train-only baseline)\n")
    lines.append(f"Boundary (LOCKED, trade-blind): **{cfg.TRAIN_HOLDOUT_BOUNDARY}** "
                 "— TRAIN before, HOLDOUT on/after (holdout untouched here). Frozen "
                 "params, judge OFF, net of MEASURED bid/ask. Generated by "
                 "`python run.py --oanda-phase-a`.\n")

    # --- Gate 1 headline
    lines.append("## Gate 1 — dual-confluence (score==100), TRAIN only\n")
    lines.append("| Metric | Value |")
    lines.append("|--------|------:|")
    lines.append(f"| Resolved trades (n) | {g['n_resolved']} |")
    lines.append(f"| Still open at data end | {g['n_still_open']} |")
    lines.append(f"| Wins / Losses | {g['wins']} / {g['losses']} |")
    lines.append(f"| Win rate | {_fmt_pct(g['win_rate'])} |")
    lines.append(f"| Mean R (net of measured spread) | {_fmt_r(g['mean_r'])} |")
    lines.append(f"| Total R | {g['total_r']:+.2f} |")
    if g["ci"]:
        c = g["ci"]
        lines.append(f"| Bootstrap mean | {_fmt_r(c['mean'])} |")
        lines.append(f"| One-sided 95% lower bound | {_fmt_r(c['one_sided_95_lower'])} |")
        lines.append(f"| Two-sided 95% CI | [{c['two_sided_95_lower']:+.3f}, "
                     f"{c['two_sided_95_upper']:+.3f}] R |")

    non_neg = g["mean_r"] is not None and g["mean_r"] >= 0
    lines.append("\n### Gate 1 verdict\n")
    if g["mean_r"] is None:
        verdict = ("**INSUFFICIENT DATA** — no resolved dual-confluence trades in "
                   "the train window. Cannot read the gate; see integrity notes.")
    elif non_neg:
        verdict = (f"**PASS (non-negative)** — train baseline mean is {_fmt_r(g['mean_r'])} "
                   "net of measured spread. Phase A's gate is cleared; Phase B (a small, "
                   "pre-registered train-only fit) is the next authorised step. Note this "
                   "is a train baseline, NOT evidence of an edge — the registered test is "
                   f"the Phase C holdout at n>={g['registered_n']}.")
    else:
        verdict = (f"**STOP — FAILS GATE 1.** Train baseline mean is {_fmt_r(g['mean_r'])} "
                   "net of real spread: negative on the clean data's own training half, "
                   "before any out-of-sample test. Per the scope this ends Phase A — no "
                   "parameter changes, no retest-window/vol-scale attempts, no Phase B.")
    lines.append(verdict + "\n")
    if g["n_resolved"] < g["registered_n"]:
        lines.append(f"> Power note: n={g['n_resolved']} resolved here is below the "
                     f"registered n>={g['registered_n']} — Gate 1 is a directional sanity "
                     "check on TRAIN, not the registered decision (that is the holdout).\n")

    # --- Integrity
    lines.append("## Data integrity\n")
    lines.append("### Coverage & the 2021-01-01 split\n")
    lines.append("| Pair | Bars | First | Last | Train | Holdout | Train% | Holdout% |")
    lines.append("|------|-----:|-------|------|------:|--------:|-------:|---------:|")
    for i in integ:
        lines.append(f"| {i['symbol']} | {i['n_bars']} | {i['first']} | {i['last']} | "
                     f"{i['n_train']} | {i['n_holdout']} | {i['train_frac']*100:.0f}% | "
                     f"{i['holdout_frac']*100:.0f}% |")
    if integ:
        tf = mean(i["train_frac"] for i in integ) * 100
        hf = mean(i["holdout_frac"] for i in integ) * 100
        split_ok = 60 <= tf <= 80
        lines.append(f"\n_Basket-average split: **{tf:.0f}% train / {hf:.0f}% holdout** "
                     f"(target ~70/30). {'Sane.' if split_ok else 'OUT OF TARGET RANGE — see note.'}_\n")
        if not split_ok:
            lines.append("> ⚠️ The locked 2021-01-01 boundary does not produce a ~70/30 split "
                         "on the obtainable history. Per protocol I have NOT adjusted it and NOT "
                         "used trade counts to move it — flagging for a fresh trade-blind date.\n")

    lines.append("### Real opens? (close-open)/open — the thing Yahoo faked\n")
    lines.append("Yahoo daily opens were ~0.01% median (degenerate). Real OANDA opens "
                 "should be materially non-zero.\n")
    lines.append("| Pair | Median move | Mean | p90 | Zero-open frac |")
    lines.append("|------|------------:|-----:|----:|---------------:|")
    for i in integ:
        o = i["opens_all"]
        lines.append(f"| {i['symbol']} | {o['median_pct']:.4f}% | {o['mean_pct']:.4f}% | "
                     f"{o['p90_pct']:.4f}% | {o['zero_open_frac']:.3f} |")

    lines.append("\n### Far-end spot check (oldest 20 bars) — synthetic history?\n")
    lines.append("| Pair | Far median open-move | Far median spread (pips) |")
    lines.append("|------|---------------------:|-------------------------:|")
    for i in integ:
        fo = i["opens_far_end"]
        fs = i["spread_far_end"]
        fs_med = f"{fs['median']:.2f}" if fs else "n/a"
        lines.append(f"| {i['symbol']} | {fo['median_pct']:.4f}% | {fs_med} |")

    lines.append("\n### Measured spread vs assumed table (pips)\n")
    lines.append("Spread is measured at each daily candle's CLOSE (21:00 UTC). Verified "
                 "empirically that the close is a structurally TIGHT moment (~1.5 pips on "
                 "EUR_USD today, matching the H1 intraday spread) — it is the daily *open* "
                 "that rollover-spikes to 7-10 pips, and that is NOT used. So the medians "
                 "below are honest; the wider tail (p90) is real historical/Friday-close "
                 "widening, not a clamp. The charge is real, if slightly conservative vs "
                 "modern spreads.\n")
    lines.append("| Pair | Measured median | Measured p90 | Assumed (old table) |")
    lines.append("|------|----------------:|-------------:|--------------------:|")
    for i in integ:
        s = i["spread_all"]
        med = f"{s['median']:.2f}" if s else "n/a"
        p90 = f"{s['p90']:.2f}" if s else "n/a"
        lines.append(f"| {i['symbol']} | {med} | {p90} | {i['assumed_spread']:.2f} |")

    lines.append(f"\n> Modelling notes: (1) the detectors see a trailing "
                 f"{cfg.FX_LIVE_DAILY_LOOKBACK}-bar (~3yr) window per decision — the SAME "
                 "span the live bot fetches (period=\"3y\") — so this is faithful to "
                 "production, not the full 15-20yr history. (2) Entry is worsened by the "
                 "full measured spread and R:R is post-spread (existing honest machinery); "
                 "resolution walks daily high/low. Daily granularity is deliberate for "
                 "Phase A-C (scope §1); intraday resolution is Phase D, only if the holdout "
                 "passes.\n")

    text = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    log.info("wrote %s", path)
    return text


def main() -> dict[str, Any]:
    data = asyncio.run(run_phase_a())
    write_report(data)
    return data
