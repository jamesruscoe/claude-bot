"""Per-pattern confidence — FORWARD-ONLY, from measured expectancy, never shape.

PATTERN_SCOPE.md §3.4. A firing pattern's confidence is a pure function of its OWN
realized track record on out-of-sample (forward) trades: how many resolved, their
mean R, and the one-sided 95% bootstrap lower bound. It is NOT a function of how
clean the chart shape looks, and it does NOT use in-sample (train/backfill)
expectancy — patterns are calibrated on train, so train expectancy is inflated by
construction and would launder that inflation into the number the email shows.

Tiers:
  unproven      n < FX_CONF_MIN_N (30)                  -> probation size; real money: DO NOT TRADE
  provisional   FX_CONF_MIN_N <= n < FX_CONF_PROVEN_N   -> small size (SE~0.31R at n=30: NOT validation)
  proven        n >= FX_CONF_PROVEN_N and LB > 0        -> scale up
  not_positive  n >= FX_CONF_PROVEN_N and LB <= 0       -> candidate for auto-disable
"""
from __future__ import annotations

from typing import Any

from v2 import config as cfg
from v2 import store
from v2.stats import bootstrap_mean_ci


def _forward_r(pattern: str) -> list[float]:
    """Unsized R of RESOLVED forward trades for `pattern` (out-of-sample only)."""
    rows = store.trades_by_pattern(pattern, source="forward")
    out: list[float] = []
    for t in rows:
        if t.get("outcome") is None:
            continue
        r = t.get("raw_r")
        if r is not None:
            out.append(float(r))
    return out


def confidence_for(pattern: str) -> dict[str, Any]:
    """Forward-only confidence summary for one pattern. Pure read of the ledger."""
    rs = _forward_r(pattern)
    n = len(rs)
    ci = bootstrap_mean_ci(rs) if n >= 2 else None
    mean_r = ci["mean"] if ci else (rs[0] if rs else None)
    lb = ci["one_sided_95_lower"] if ci else None
    wins = sum(1 for r in rs if r > 0)
    win_rate = (wins / n) if n else None

    if n < cfg.FX_CONF_MIN_N:
        tier = "unproven"
    elif n < cfg.FX_CONF_PROVEN_N:
        tier = "provisional"
    elif lb is not None and lb > 0:
        tier = "proven"
    else:
        tier = "not_positive"

    return {"pattern": pattern, "n": n, "wins": wins, "win_rate": win_rate,
            "mean_r": mean_r, "lower_bound": lb, "total_r": sum(rs) if rs else 0.0,
            "tier": tier}


def label(pattern: str) -> str:
    """Short human/email label, e.g. 'unproven (n=7 fwd)' or 'proven (+0.21R, n=163)'."""
    c = confidence_for(pattern)
    n, tier = c["n"], c["tier"]
    if tier == "unproven":
        return f"unproven (n={n} fwd)"
    if tier == "provisional":
        return f"provisional (n={n}, not yet validated)"
    mr = c["mean_r"]
    if tier == "proven":
        return f"proven ({mr:+.2f}R, n={n})"
    return f"not positive ({mr:+.2f}R, n={n}) — disable candidate"


def all_patterns_report() -> list[dict[str, Any]]:
    """Confidence + expectancy for every pattern that has any ledger trades,
    plus every currently-enabled pattern (so freshly-enabled ones show as n=0)."""
    names = set(store.distinct_patterns()) | {
        p for p, on in cfg.FX_PATTERNS.items() if on}
    return [confidence_for(p) for p in sorted(names)]


def pattern_report_text() -> str:
    """Per-pattern expectancy table (FORWARD/out-of-sample only) — the 'keep what
    works, drop what doesn't' view. Confidence is derived from these numbers."""
    def _pct(x): return f"{x * 100:.0f}%" if x is not None else "n/a"
    def _r(x): return f"{x:+.2f}R" if x is not None else "n/a"
    rows = all_patterns_report()
    lines = ["# Per-pattern expectancy (FORWARD / out-of-sample only)\n",
             "Confidence is a function of THESE numbers (expectancy + n), never shape. "
             f"unproven if n < {cfg.FX_CONF_MIN_N}; proven needs n >= {cfg.FX_CONF_PROVEN_N} "
             "AND one-sided 95% lower bound > 0.\n",
             "| Pattern | Enabled | n (fwd) | Win rate | Mean R | 95% LB | Total R | Confidence |",
             "|---------|:-------:|--------:|---------:|-------:|-------:|--------:|------------|"]
    for c in rows:
        en = "on" if cfg.fx_pattern_enabled(c["pattern"]) else "off"
        lines.append(
            f"| {c['pattern']} | {en} | {c['n']} | {_pct(c['win_rate'])} | "
            f"{_r(c['mean_r'])} | {_r(c['lower_bound'])} | {c['total_r']:+.2f} | {c['tier']} |")
    if not rows:
        lines.append("| _(no patterns with trades yet)_ | | | | | | | |")
    lines.append("\n> FORWARD-only by construction: train/backfill trades are excluded so "
                 "in-sample calibration cannot inflate a pattern's confidence. Real money: "
                 "`unproven` = do not trade (PATTERN_SCOPE §3.4).\n")
    return "\n".join(lines)
