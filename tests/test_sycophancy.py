"""Agreement never substitutes for factual accuracy or task benefit."""
from __future__ import annotations

import random

import pytest

from commontrace import sycophancy


def traffic(rng, effects, *, n=800, propensity=.7):
    result = []
    for i in range(n):
        injected = rng.random() < propensity
        values = {name: int(rng.random() < .5+effect/2*(1 if injected else -1))
                  for name, effect in zip(sycophancy.METRICS, effects)}
        result.append({"occasion_id": str(i), "query_id": "q"+str(i%2), "injected": injected,
                       "rate": 1-propensity, **values})
    return result


def test_seeded_flattering_power_and_helpful_false_flag_rate():
    rng = random.Random(1227)
    flattering = helpful = 0
    for _ in range(40):
        flattering += sycophancy.audit(traffic(rng, (0, 0, .8)), horizon=800,
                                       equivalence_margin=.25, queries=["q0", "q1"])["flattering"]
        helpful += sycophancy.audit(traffic(rng, (.6, .6, .8)), horizon=800,
                                    equivalence_margin=.25, queries=["q0", "q1"])["flattering"]
    assert flattering >= 34
    assert helpful/40 <= .05


def test_seeded_three_effects_are_unbiased_and_intervals_cover_together():
    rng = random.Random(33)
    effects = (.3, -.2, .6)
    totals = [0., 0., 0.]
    covered = 0
    for _ in range(40):
        report = sycophancy.audit(traffic(rng, effects, n=600), horizon=600, queries=["q0", "q1"])
        estimates = report["metrics"]
        for i, name in enumerate(sycophancy.METRICS):
            totals[i] += estimates[name]["estimate"]
        covered += all(estimates[name]["interval"][0] <= effect <= estimates[name]["interval"][1]
                       for name, effect in zip(sycophancy.METRICS, effects))
    assert [total/40 for total in totals] == pytest.approx(effects, abs=.025)
    assert covered >= 38


def test_fixed_horizon_does_not_turn_non_significance_into_equivalence():
    short = traffic(random.Random(8), (0, 0, .8), n=100)
    report = sycophancy.audit(short, horizon=800, queries=["q0", "q1"])
    assert not report["decision_ready"] and not report["flattering"]
    assert not report["accuracy_nonnegative"]
    assert report["metrics"]["accuracy"]["interval"] == [-1, 1]
    with pytest.raises(ValueError, match="schedule"):
        sycophancy.audit(list(reversed(short)), horizon=100, queries=["q0", "q1"])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 2, "1"])
def test_secondary_metrics_are_bounded_and_named(value):
    with pytest.raises(ValueError):
        sycophancy.metrics({"accuracy": value, "agreement": 1})
    with pytest.raises(ValueError):
        sycophancy.metrics({"accuracy": 1, "satisfaction": 1})
