"""AIPW effect estimates with an asymptotic confidence sequence, valid when holdout rates change.

Stdlib only (the Hub server image runs `experiment.analyze`, which uses this).
See `allocation` for the design and the references.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

WINDOW = 400
BURN_IN = 50


def _norm_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def _norm_ppf(p: float) -> float:
    from commontrace import experiment

    return experiment._norm_ppf(p)


def _mixture_rho2(alpha: float, horizon: float) -> float:
    """The normal-mixture precision that makes the sequence tightest at `horizon` unit-variance steps."""
    log_alpha = math.log(alpha)
    return (-2.0 * log_alpha + math.log(1.0 - 2.0 * log_alpha)) / max(1.0, horizon)


def confidence_sequence(scores: list[float], alpha: float = 0.05,
                        horizon: float = 2000.0) -> tuple[float, float, float]:
    """(mean, low, high): a two-sided asymptotic confidence sequence for the mean of `scores`.

    ``mean +- sqrt(2 (V rho^2 + 1) / rho^2 * log(sqrt(V rho^2 + 1) / alpha)) / t`` with
    ``V = t * sample variance``; ``rho`` is fixed by ``alpha`` and ``horizon`` before any data.
    """
    t = len(scores)
    if t < 2:
        return (scores[0] if scores else 0.0), -math.inf, math.inf
    mean = math.fsum(scores) / t
    variance = math.fsum((s - mean) ** 2 for s in scores) / (t - 1)
    rho2 = _mixture_rho2(alpha, horizon)
    v = t * variance * rho2 + 1.0
    radius = math.sqrt(2.0 * v / rho2 * math.log(math.sqrt(v) / alpha)) / t
    return mean, mean - radius, mean + radius


def running_intersection(scores: list[float], alpha: float = 0.05, horizon: float = 2000.0,
                         burn_in: int = BURN_IN) -> tuple[float, float, float]:
    """The confidence sequence intersected over every prefix from `burn_in` scores on.

    A time-uniform sequence covers at every time at once, so its running intersection
    covers too, and never widens: a verdict reached once is not undone when a later
    era's lower propensities make the per-occasion scores noisier.
    """
    t = len(scores)
    mean, low, high = confidence_sequence(scores, alpha, horizon)
    if t <= burn_in:
        return mean, low, high
    rho2 = _mixture_rho2(alpha, horizon)
    total = math.fsum(scores[:burn_in - 1])
    square = math.fsum(s * s for s in scores[:burn_in - 1])
    for k in range(burn_in, t + 1):
        s = scores[k - 1]
        total += s
        square += s * s
        m = total / k
        v = k * max(0.0, (square - k * m * m) / (k - 1)) * rho2 + 1.0
        radius = math.sqrt(2.0 * v / rho2 * math.log(math.sqrt(v) / alpha)) / k
        low, high = max(low, m - radius), min(high, m + radius)
    if low > high:  # the sequences disagree: only possible on a miss, so report the latest
        return confidence_sequence(scores, alpha, horizon)
    return mean, low, high


def fixed_interval(scores: list[float], alpha: float = 0.05) -> tuple[float, float, float, float]:
    """(mean, low, high, two-sided p) for ONE look: the central-limit interval of the AIPW mean."""
    t = len(scores)
    if t < 2:
        return (scores[0] if scores else 0.0), -math.inf, math.inf, 1.0
    mean = math.fsum(scores) / t
    se = math.sqrt(math.fsum((s - mean) ** 2 for s in scores) / (t - 1) / t)
    z = _norm_ppf(1.0 - alpha / 2.0)
    if se == 0.0:
        return mean, mean, mean, (1.0 if mean == 0.0 else 0.0)
    p = 2.0 * (1.0 - _norm_cdf(abs(mean) / se))
    return mean, mean - z * se, mean + z * se, p


@dataclass(frozen=True)
class Observation:
    injected: bool
    succeeded: bool
    rate: float  # probability of being withheld, as logged
    stratum: str = ""


class _RunningMean:
    """Mean of the last `window` outcomes, shrunk toward 1/2 by one pseudo-observation per side."""

    def __init__(self, window: int):
        self.values: deque[float] = deque(maxlen=window)
        self.total = 0.0

    def get(self, fallback: float | None = None) -> float:
        prior = 0.5 if fallback is None else fallback
        return (self.total + 2.0 * prior) / (len(self.values) + 2.0)

    def add(self, value: float) -> None:
        if len(self.values) == self.values.maxlen:
            self.total -= self.values[0]
        self.values.append(value)
        self.total += value


def aipw_scores(observations: list[Observation], window: int = WINDOW) -> list[float]:
    """One AIPW pseudo-outcome per randomized observation, in the order given.

    Observations whose rate is 0 or 1 were not randomized and carry no
    information about the effect; they are skipped.
    """
    pooled = {True: _RunningMean(window), False: _RunningMean(window)}
    by_stratum: dict[tuple[str, bool], _RunningMean] = {}
    scores = []
    for o in observations:
        if not 0.0 < o.rate < 1.0:
            continue
        means = {}
        for arm in (True, False):
            overall = pooled[arm].get()
            means[arm] = by_stratum[(o.stratum, arm)].get(overall) if (o.stratum, arm) in by_stratum else overall
        y = 1.0 if o.succeeded else 0.0
        pi = 1.0 - o.rate
        if o.injected:
            scores.append(means[True] - means[False] + (y - means[True]) / pi)
        else:
            scores.append(means[True] - means[False] - (y - means[False]) / o.rate)
        pooled[o.injected].add(y)
        if o.stratum:
            by_stratum.setdefault((o.stratum, o.injected), _RunningMean(window)).add(y)
    return scores


@dataclass(frozen=True)
class Estimate:
    effect: float
    ci_low: float
    ci_high: float
    p_value: float
    n_injected: int
    n_withheld: int
    method: str


def estimate(observations: list[Observation], *, alpha: float = 0.05, horizon: float = 2000.0,
             sequential: bool = True, window: int = WINDOW) -> Estimate:
    scores = aipw_scores(observations, window)
    randomized = [o for o in observations if 0.0 < o.rate < 1.0]
    n_inj = sum(o.injected for o in randomized)
    _mean, low, high, p = fixed_interval(scores, alpha)
    mean = _mean
    if sequential:
        mean, low, high = running_intersection(scores, alpha, horizon)
        if len(scores) < BURN_IN:
            # The sequence's coverage is asymptotic; before the burn-in it claims nothing.
            low, high = -math.inf, math.inf
    return Estimate(mean, max(-1.0, low), min(1.0, high), p, n_inj, len(randomized) - n_inj,
                    "aipw-asymptotic-cs" if sequential else "aipw-clt")
