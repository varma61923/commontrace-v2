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

from unimem import aipw
from unimem import frontier_protocol as fp


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


def _adjusted(observations: list[aipw.Observation],
              prior_success: Sequence[float] | None) -> tuple[list[float], list[float]]:
    """Predictable AIPW scores and their design bounds, optionally regression-adjusted.

    Context labels enter through ``Observation.stratum``: the AIPW arm means are
    estimated per stratum from strictly preceding outcomes. A pre-treatment prior
    success rate enters as a control variate whose slope is the least-squares
    slope of outcome on prior over *preceding* rows only, clipped to [-1, 1]. Its
    multiplier has conditional mean zero under the logged propensity, so the
    adjusted score stays conditionally unbiased for every predictable slope.
    """
    fp.count(len(observations), 0, fp.MAX_ROWS)
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
    bounds = []
    n = sb = sy = sbb = sby = 0.0
    for i, (row, score) in enumerate(zip(usable, scores)):
        p = 1 - row.rate
        bound = 1 / min(p, row.rate)
        if prior_success is not None:
            b, y = baseline[i], float(row.succeeded)
            variance = sbb - sb * sb / n if n else 0.0
            slope = (sby - sb * sy / n) / variance if n >= 10 and variance > 1e-9 else 0.0
            slope = max(-1.0, min(1.0, slope))
            # Predictable control variate: conditional expectation zero.
            scores[i] = score - slope * (int(row.injected) / p - int(not row.injected) / row.rate) * (b - .5)
            bound += .5 / min(p, row.rate)
            n, sb, sy, sbb, sby = n + 1, sb + b, sy + y, sbb + b * b, sby + b * y
        bounds.append(bound)
    return scores, bounds


def _wealth(scores: Sequence[float], bounds: Sequence[float], *, delta: float, direction: str) -> Evidence:
    total, squares, log_e, peak = 0.0, 1.0, 0.0, 1.0
    for score, design_bound in zip(scores, bounds):
        bound = design_bound + abs(delta)
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
    return Evidence(math.exp(min(log_e, 690)), log_e, peak, len(scores), direction, delta)


def process(observations: list[aipw.Observation], *, delta: float = 0.0,
            direction: str = "helps", prior_success: Sequence[float] | None = None) -> Evidence:
    fp.number(delta, -1, 1, "null margin")
    if direction not in ("helps", "hurts"):
        raise ValueError("direction must be helps or hurts")
    scores, bounds = _adjusted(observations, prior_success)
    return _wealth(scores, bounds, delta=delta, direction=direction)


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


def report(assignments: Iterable[Any], *, strict: bool = True) -> dict[str, dict[str, Any]]:
    """Per-memory directional e-values from logged assignments, in log order.

    ``strict`` refuses duplicate (memory, occasion) rows. The evidence of record
    (`experiment.analyze`) passes ``strict=False``: a duplicate is then dropped
    (the first row is kept, which never adds evidence) and counted, so a dirty
    log is reported rather than taking the whole analysis down. Rows without a
    logged propensity cannot carry this guarantee and are skipped.
    """
    grouped: dict[str, list[aipw.Observation]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    duplicates: dict[str, int] = defaultdict(int)
    for i, row in enumerate(assignments):
        fp.count(i, 0, fp.MAX_ROWS - 1)
        if row.succeeded is None:
            continue
        lesson = str(getattr(row, "lesson", getattr(row, "lesson_slug", "")))
        key = (lesson, row.occasion_id)
        if key in seen:
            if strict:
                raise ValueError("duplicate occasion in e-process evidence")
            duplicates[lesson] += 1
            continue
        seen.add(key)
        rate = getattr(row, "rate", None)
        if rate is None:
            continue  # Historical records without propensities cannot carry this guarantee.
        injected, succeeded = row.injected, row.succeeded
        if not strict and injected in (0, 1) and succeeded in (0, 1):
            injected, succeeded = bool(injected), bool(succeeded)
        grouped[lesson].append(aipw.Observation(injected, succeeded, rate, getattr(row, "context", "") or ""))
    result = {}
    for lesson, observations in sorted(grouped.items()):
        scores, bounds = _adjusted(observations, None)
        helps = _wealth(scores, bounds, delta=0.0, direction="helps")
        hurts = _wealth(scores, bounds, delta=0.0, direction="hurts")
        result[lesson] = {"e_value": (helps.e_value + hurts.e_value) / 2,
                          "helps": helps.e_value, "hurts": hurts.e_value, "n": helps.n,
                          "assumptions": helps.kind,
                          "e_value_null": "effect equals zero; helps/hurts fields have separate directional nulls"}
        if duplicates.get(lesson):
            result[lesson]["duplicates_dropped"] = duplicates[lesson]
    return result
