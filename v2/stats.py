"""Small shared statistics — the deterministic bootstrap used for both the OANDA
registered criterion and per-pattern confidence (PATTERN_SCOPE.md). Deterministic
(fixed seed) so a CI is reproducible run-to-run."""
from __future__ import annotations

import random
from statistics import mean

_BOOTSTRAP_ITERS = 10_000
_BOOTSTRAP_SEED = 20260721


def bootstrap_mean_ci(values: list[float], *, iters: int = _BOOTSTRAP_ITERS
                      ) -> dict[str, float] | None:
    """Percentile bootstrap of the mean. Returns the point mean, the one-sided
    95% lower bound (5th pct — the registered test), and the two-sided 95%
    interval. None if fewer than 2 values."""
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
