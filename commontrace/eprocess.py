"""Predictable AIPW betting evidence and e-BH under arbitrary dependence.

Methods: Waudby-Smith & Ramdas (arXiv:2010.09686), Wang & Ramdas
(arXiv:2009.02824), Ramdas & Wang (arXiv:2410.23614). Pre-treatment regression
adjustment follows the predictable-estimation principle of Lindon et al.
(arXiv:2210.08589), not a claim to reproduce their Gaussian linear-model method.

The product is a finite-sample e-process when the logged propensity is exact,
outcomes are bounded binary, each task is observed once, missingness is ignorable,
and the tested directional null holds conditionally at every occasion. It is
not a test of an unrestricted time-average null with arbitrarily changing signs.
Products across deployments require conditional e-validity (e.g. independence);
an arithmetic mean is valid under arbitrary dependence. Different memories may
share outcomes: e-BH still controls FDR at each decision time, including one joint
stopping time. Repeated separate e-BH releases require an online FDR procedure.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from commontrace import aipw
from commontrace import frontier_protocol as fp


@dataclass(frozen=True)
class Evidence:
    e_value: float
    log_e_value: float
    maximum: float
    n: int
    direction: str
    delta: float
    kind: str = "guarantee under conditional directional null and exact propensities"


def ebh(values: Sequence[float], alpha: float = 0.05) -> list[bool]:
    fp.number(alpha, 1e-12, 1 - 1e-12, "alpha")
    fp.count(len(values), 0, 100_000)
    for value in values:
        fp.number(value, 0, 1e300, "e-value")
    order = sorted(range(len(values)), key=lambda i: (-values[i], i))
    cutoff = 0
    for rank, index in enumerate(order, 1):
        if values[index] >= len(values) / (alpha * rank):
            cutoff = rank
    rejected = set(order[:cutoff])
    return [i in rejected for i in range(len(values))]


def process(observations: list[aipw.Observation], *, delta: float = 0.0,
            direction: str = "helps", prior_success: Sequence[float] | None = None) -> Evidence:
    fp.number(delta, -1, 1, "null margin")
    fp.count(len(observations), 0, fp.MAX_ROWS)
    if direction not in ("helps", "hurts"):
        raise ValueError("direction must be helps or hurts")
    if prior_success is not None and len(prior_success) != len(observations):
        raise ValueError("one pre-treatment prior required per observation")
    usable = []
    baseline = []
    for i, row in enumerate(observations):
        fp.number(row.rate, 0, 1, "logged holdout propensity")
        if type(row.injected) is not bool or type(row.succeeded) is not bool:
            raise ValueError("assignment and outcome must be binary")
        if row.rate in (0, 1):
            continue
        usable.append(row)
        if prior_success is not None:
            baseline.append(fp.number(prior_success[i], 0, 1, "pre-treatment prior"))
    # Existing AIPW nuisance means are estimated on strictly preceding outcomes.
    scores = aipw.aipw_scores(usable)
    total, squares, log_e, peak = 0.0, 1.0, 0.0, 1.0
    for i, (row, score) in enumerate(zip(usable, scores)):
        p = 1 - row.rate
        bound = 1 / min(p, row.rate) + abs(delta)
        if prior_success is not None:
            # Predictable control variate has conditional expectation zero.
            correction = (int(row.injected) / p - int(not row.injected) / row.rate) * (baseline[i] - .5)
            score -= correction
            bound += .5 / min(p, row.rate)
        value = (score if direction == "helps" else -score) - delta
        # All ingredients are measured before the current outcome is inspected.
        bet = min(.5 / bound, max(.02 / bound, total / squares))
        factor = 1 + bet * value
        if factor <= 0 or not math.isfinite(factor):
            raise ValueError("score violates predictable betting bounds")
        log_e += math.log1p(bet * value)
        total += value
        squares += value * value
        peak = max(peak, math.exp(min(log_e, 690)))
    return Evidence(math.exp(min(log_e, 690)), log_e, peak, len(usable), direction, delta)


def combine(values: Sequence[float], *, independent: bool = False) -> float:
    fp.count(len(values), 1, 10_000)
    for value in values:
        fp.number(value, 0, 1e300, "e-value")
    if independent:
        if any(v == 0 for v in values):
            return 0.0
        return math.exp(min(690, math.fsum(math.log(v) for v in values)))
    return math.fsum(v / len(values) for v in values)


def interval(scores: Sequence[float], *, bound: float, alpha: float = .05) -> tuple[float, float, float]:
    """Finite-sample betting confidence sequence for a stationary score mean.

    Predictable bets use preceding raw scores, so the inverted wealth is
    monotone in the null mean. No Gaussian approximation or fitted variance
    is used to assert coverage. `bound` must be fixed by the trial design.
    """
    fp.number(bound, 1, 1e6, "design score bound")
    fp.number(alpha, 1e-9, .99)
    fp.count(len(scores), 0, fp.MAX_ROWS)
    for value in scores:
        fp.number(value, -bound, bound, "DR score")
    if not scores:
        return 0, -1, 1

    def lower(values: Sequence[float]) -> float:
        bets = []
        total, squares = 0.0, 1.0
        for x in values:
            bets.append(min(.5 / (bound + 1), max(.01 / (bound + 1), total / squares)))
            total += x
            squares += x * x

        def rejects(null: float) -> bool:
            log_wealth = math.fsum(math.log1p(bet * (x - null)) for bet, x in zip(bets, values))
            return log_wealth >= math.log(2 / alpha)

        low, high = -1.0, 1.0
        if not rejects(low):
            return low
        for _ in range(40):
            mid = (low + high) / 2
            if rejects(mid):
                low = mid
            else:
                high = mid
        return low

    return math.fsum(scores) / len(scores), lower(scores), -lower([-value for value in scores])


def report(assignments: Iterable[Any]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[aipw.Observation]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for i, row in enumerate(assignments):
        fp.count(i, 0, fp.MAX_ROWS - 1)
        if row.succeeded is None:
            continue
        lesson = str(getattr(row, "lesson", getattr(row, "lesson_slug", "")))
        key = (lesson, row.occasion_id)
        if key in seen:
            raise ValueError("duplicate occasion in e-process evidence")
        seen.add(key)
        rate = getattr(row, "rate", None)
        if rate is None:
            continue  # Historical records without propensities cannot carry this guarantee.
        grouped[lesson].append(aipw.Observation(row.injected, row.succeeded, rate, getattr(row, "context", "")))
    result = {}
    for lesson, observations in sorted(grouped.items()):
        helps, hurts = process(observations), process(observations, direction="hurts")
        result[lesson] = {"e_value": (helps.e_value + hurts.e_value) / 2,
                          "helps": helps.e_value, "hurts": hurts.e_value, "n": helps.n,
                          "assumptions": helps.kind,
                          "e_value_null": "effect equals zero; helps/hurts fields have separate directional nulls"}
    return result
