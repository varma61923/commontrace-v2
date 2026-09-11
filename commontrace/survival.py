"""Right-censored survival analysis, for telling "not yet" apart from "never".

`integrity.check_differential_attrition` asks whether both arms are equally
likely to have an outcome recorded, and it is the load-bearing check: dropping
unresolved occasions is unbiased only when both arms drop them at the same
rate. What it could not ask, before this module existed, is WHEN.

That omission has a specific and embarrassing failure mode. An occasion is
recorded when somebody reports how it went, which happens some time AFTER the
arm was assigned -- minutes for a clean resolution, days for one that gets
escalated, never for one that is abandoned. A lesson that genuinely helps makes
its arm conclude SOONER. So at any moment before every occasion has run its
course, the injected arm has more outcomes on the books than the withheld arm,
purely because it got there first. The terminal-rate test sees a gap, calls it
differential attrition, and returns INVALIDATES -- suppressing the fleet's
effect estimate at exactly the moment the memory is working. The better the
lesson, the sooner it is disqualified.

The distinction the estimate actually needs is between two different reasons an
occasion has no outcome:

  administrative censoring   It was assigned recently and has not had time to
                             conclude. Benign: wait, and it resolves. Carries
                             no information about the treatment.
  loss to follow-up          It has had every chance and nothing ever came
                             back. Malign: if one arm loses more of these, the
                             survivors are selected on something downstream of
                             the treatment.

Only the second is attrition. Telling them apart is what survival analysis is
for, and both estimators here are the standard ones:

  `kaplan_meier`    the product-limit estimate of S(t) -- the probability an
                    occasion is STILL unreported t seconds after assignment --
                    which handles censoring correctly by removing censored
                    occasions from the risk set at the moment they are censored
                    rather than counting them as failures.
  `log_rank_test`   the standard test for whether two arms share one reporting
                    hazard, comparing observed against expected events at every
                    distinct event time and pooling them.

No SciPy, no lifelines, nothing new in requirements.txt: the rest of this
package computes its own two-proportion test, Benjamini-Hochberg correction and
normal CDF by hand for the same reason -- a statistics dependency that has to
be installed is a statistic that silently does not run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from commontrace.experiment import _norm_cdf


@dataclass(frozen=True)
class Observation:
    """One occasion's follow-up: how long it was watched, and how it ended.

    `duration` is seconds from arm assignment to either the outcome being
    recorded (`event=True`) or to the moment the log was read (`event=False`,
    still waiting). A negative duration is not representable -- callers clamp
    at zero, because clock skew between the app and the database is ordinary
    and must not become a negative time-to-event.
    """

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
    """S(t): the probability an occasion is still unreported at t.

    The product-limit estimator, which is just the chain rule applied to
    "survived every event time so far":

        S(t) = prod over event times t_i <= t of (1 - d_i / n_i)

    where `d_i` is how many occasions were reported at t_i and `n_i` is how
    many were still waiting immediately before it. Censored occasions -- the
    ones still pending when the log was read -- sit in `n_i` up until their own
    censoring time and then leave without ever counting as an event. That is
    the whole trick, and it is why a run read early does not look like a run
    where everybody vanished.

    Returns one Step per distinct event time, in ascending order. Times where
    only censoring happened produce no step (S does not move) but do shrink the
    risk set for every later step.
    """
    if not observations:
        return []
    ordered = sorted(observations, key=lambda o: (o.duration, not o.event))
    steps: list[Step] = []
    at_risk = len(ordered)
    survival = 1.0
    i = 0
    while i < len(ordered):
        t = ordered[i].duration
        # Everything recorded at exactly this instant is one rung; everything
        # censored at this instant leaves the risk set without moving S.
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
    """The earliest t by which `fraction` of occasions had been reported.

    `None` when the curve never gets there -- which is itself the answer worth
    having: a run where only 60% of occasions are ever reported has no
    90th-percentile reporting time, and pretending otherwise would invent a
    follow-up horizon out of data that does not support one.
    """
    for step in curve:
        if 1.0 - step.survival >= fraction:
            return step.time
    return None


def log_rank_test(
    group_a: list[Observation], group_b: list[Observation]
) -> tuple[float, float]:
    """Do these two arms report on the same schedule? Returns (z, p).

    At every distinct time where anything was reported, the null hypothesis
    predicts how many of those reports should have come from arm A, given only
    how many of each arm were still waiting:

        E_a = n_a * d / n          V_a = n_a * n_b * d * (n - d) / (n^2 * (n - 1))

    Summing the observed-minus-expected across all event times and standardising
    by the summed variance gives a statistic that is standard normal under the
    null. Two-sided, because either arm reporting faster is worth knowing: the
    injected arm racing ahead is evidence the lesson concludes work sooner,
    while the withheld arm racing ahead is a warning that the control is being
    closed out early.

    Returns (0.0, 1.0) when there is nothing to compare -- an empty arm, or no
    reported outcomes at all -- rather than raising, matching the convention
    every other check in this package follows for "not answerable yet".
    """
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
        # The hypergeometric variance. Undefined at n == 1 (guarded above) and
        # zero when every occasion still waiting is reported at once (d == n),
        # which contributes nothing rather than dividing by zero.
        variance += (n_a * n_b * d * (n - d)) / (n * n * (n - 1))

    if variance <= 0:
        return 0.0, 1.0
    z = observed_minus_expected / math.sqrt(variance)
    p = 2.0 * (1.0 - _norm_cdf(abs(z)))
    return z, max(0.0, min(1.0, p))
