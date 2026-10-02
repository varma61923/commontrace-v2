"""Right-censored survival analysis, for telling "not yet" apart from "never"."""

from __future__ import annotations

import math
from dataclasses import dataclass

from commontrace.experiment import _norm_cdf


@dataclass(frozen=True)
class Observation:
    """One occasion's follow-up: how long it was watched, and how it ended."""

    duration: float
    event: bool


@dataclass(frozen=True)
class Step:
    """One rung of a Kaplan-Meier staircase."""

    time: float
    survival: float
    at_risk: int
    events: int


def kaplan_meier(observations: list[Observation]) -> list[Step]:
    """S(t): the probability an occasion is still unreported at t."""
    if not observations:
        return []
    ordered = sorted(observations, key=lambda o: (o.duration, not o.event))
    steps: list[Step] = []
    at_risk = len(ordered)
    survival = 1.0
    i = 0
    while i < len(ordered):
        t = ordered[i].duration
        events = 0
        censored = 0
        while i < len(ordered) and ordered[i].duration == t:
            if ordered[i].event:
                events += 1
            else:
                censored += 1
            i += 1
        if events and at_risk > 0:
            survival *= 1.0 - events / at_risk
            steps.append(Step(time=t, survival=survival, at_risk=at_risk, events=events))
        at_risk -= events + censored
    return steps


def reported_fraction_at(curve: list[Step], t: float) -> float:
    """1 - S(t): the share of occasions reported by t, per the curve."""
    survival = 1.0
    for step in curve:
        if step.time > t:
            break
        survival = step.survival
    return 1.0 - survival


def time_to_reported_fraction(curve: list[Step], fraction: float) -> float | None:
    """The earliest t by which `fraction` of occasions had been reported."""
    for step in curve:
        if 1.0 - step.survival >= fraction:
            return step.time
    return None


def log_rank_test(
    group_a: list[Observation], group_b: list[Observation]
) -> tuple[float, float]:
    """Do these two arms report on the same schedule? Returns (z, p)."""
    if not group_a or not group_b:
        return 0.0, 1.0
    a_sorted = sorted(group_a, key=lambda o: o.duration)
    b_sorted = sorted(group_b, key=lambda o: o.duration)
    times = sorted({o.duration for o in a_sorted + b_sorted if o.event})
    if not times:
        return 0.0, 1.0

    observed_minus_expected = 0.0
    variance = 0.0
    for t in times:
        n_a = sum(1 for o in a_sorted if o.duration >= t)
        n_b = sum(1 for o in b_sorted if o.duration >= t)
        n = n_a + n_b
        if n <= 1:
            continue
        d_a = sum(1 for o in a_sorted if o.event and o.duration == t)
        d_b = sum(1 for o in b_sorted if o.event and o.duration == t)
        d = d_a + d_b
        if not d:
            continue
        expected_a = n_a * d / n
        observed_minus_expected += d_a - expected_a
        variance += (n_a * n_b * d * (n - d)) / (n * n * (n - 1))

    if variance <= 0:
        return 0.0, 1.0
    z = observed_minus_expected / math.sqrt(variance)
    p = 2.0 * (1.0 - _norm_cdf(abs(z)))
    return z, max(0.0, min(1.0, p))
