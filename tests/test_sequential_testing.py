from __future__ import annotations

import random

import pytest

from commontrace import experiment


def _run(seed: int, sequential: bool, lift: float, max_n: int = 1000,
         baseline: float = 0.6, step: int = 50) -> int | None:
    rng = random.Random(seed)
    observations: list[experiment.HoldoutObservation] = []
    while len(observations) < max_n:
        for _ in range(step):
            injected = rng.random() < 0.5
            rate = baseline + (lift if injected else 0.0)
            observations.append(experiment.HoldoutObservation(
                lesson_slug="L", occasion_id=f"o{len(observations)}",
                injected=injected, succeeded=rng.random() < rate,
            ))
        for effect in experiment.analyze(observations, sequential=sequential):
            if effect.verdict in (experiment.VERDICT_HELPS, experiment.VERDICT_HURTS):
                return len(observations)
    return None


def _false_positive_rate(sequential: bool, trials: int = 200) -> float:
    return sum(
        _run(seed, sequential, lift=0.0) is not None for seed in range(trials)
    ) / trials


class TestTheSpendingFunctions:
    def test_both_shapes_spend_the_whole_budget_by_the_end(self):
        for shape in experiment.SPENDING_FUNCTIONS:
            assert experiment.alpha_spent(1.0, 0.05, shape) == pytest.approx(0.05)

    def test_obrien_fleming_is_miserly_early(self):
        assert experiment.alpha_spent(0.25) < 0.001
        assert experiment.alpha_spent(0.5) < 0.01

    def test_pocock_spends_more_evenly(self):
        early_of = experiment.SPEND_POCOCK
        assert experiment.alpha_spent(0.25, shape=early_of) > experiment.alpha_spent(0.25)

    def test_spending_increases_with_information(self):
        for shape in experiment.SPENDING_FUNCTIONS:
            spent = [experiment.alpha_spent(t, shape=shape)
                     for t in (0.1, 0.3, 0.6, 0.9, 1.0)]
            assert spent == sorted(spent)

    def test_no_information_spends_nothing(self):
        assert experiment.alpha_spent(0.0) == 0.0

    def test_an_unknown_shape_is_refused(self):
        with pytest.raises(ValueError, match="unknown alpha-spending shape"):
            experiment.alpha_spent(0.5, shape="made-up")


class TestTheConfidenceSequence:
    def test_it_is_wider_than_a_fixed_n_interval_at_the_same_n(self):
        fixed = experiment.diff_confidence_interval(60, 100, 45, 100)
        anytime = experiment.anytime_confidence_interval(60, 100, 45, 100)
        assert (anytime[1] - anytime[0]) > (fixed[1] - fixed[0])

    def test_it_narrows_as_evidence_accrues(self):
        wide = experiment.anytime_confidence_interval(30, 50, 20, 50)
        narrow = experiment.anytime_confidence_interval(600, 1000, 400, 1000)
        assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])

    def test_a_degenerate_arm_yields_no_claim(self):
        lo, hi = experiment.anytime_confidence_interval(50, 50, 50, 50)
        assert lo <= 0.0 <= hi

    def test_a_degenerate_split_concludes_once_there_is_enough_of_it(self):
        small = experiment.anytime_confidence_interval(5, 5, 0, 5, target_n_per_arm=97)
        assert small[0] <= 0.0 <= small[1]
        helps = experiment.anytime_confidence_interval(60, 60, 0, 60, target_n_per_arm=97)
        assert helps[0] > 0.0
        hurts = experiment.anytime_confidence_interval(0, 60, 60, 60, target_n_per_arm=97)
        assert hurts[1] < 0.0

    def test_an_empty_arm_yields_no_claim(self):
        lo, hi = experiment.anytime_confidence_interval(0, 0, 10, 20)
        assert lo <= 0.0 <= hi

    def test_an_overwhelming_effect_still_excludes_zero(self):
        lo, hi = experiment.anytime_confidence_interval(
            48, 60, 18, 60, target_n_per_arm=98
        )
        assert lo > 0.0


class TestWhatContinuousMonitoringDoesToAFixedThreshold:
    def test_a_fixed_threshold_manufactures_results_when_watched(self):
        assert _false_positive_rate(sequential=False) > 0.15

    def test_the_sequential_boundary_holds_the_error(self):
        assert _false_positive_rate(sequential=True) <= 0.05

    def test_the_sequential_boundary_is_strictly_better_here(self):
        assert _false_positive_rate(sequential=True) < _false_positive_rate(
            sequential=False
        )


class TestItStillDetectsRealEffects:
    def test_an_effect_at_the_actionable_threshold_is_usually_found(self):
        found = sum(
            _run(seed, sequential=True, lift=experiment.DEFAULT_PRACTICAL_EFFECT)
            is not None
            for seed in range(60)
        )
        assert found / 60 >= 0.5

    def test_a_large_effect_is_always_found(self):
        found = sum(
            _run(seed, sequential=True, lift=0.25) is not None for seed in range(30)
        )
        assert found == 30

    def test_it_costs_data_rather_than_certainty(self):
        fixed = [n for n in (_run(s, False, 0.25) for s in range(30)) if n]
        sequential = [n for n in (_run(s, True, 0.25) for s in range(30)) if n]
        assert len(fixed) == len(sequential) == 30
        assert sum(sequential) / 30 > sum(fixed) / 30


class TestTheDefaultIsUnchanged:
    def test_a_one_shot_analysis_is_not_penalised(self):
        observations = []
        for i in range(200):
            observations.append(experiment.HoldoutObservation(
                "L", f"w{i}", False, i % 10 < 4))
        for i in range(200):
            observations.append(experiment.HoldoutObservation(
                "L", f"i{i}", True, i % 10 < 6))
        [effect] = experiment.analyze(observations)
        assert effect.verdict == experiment.VERDICT_HELPS

    def test_a_withheld_sequential_verdict_says_why_and_says_not_yet(self):
        observations = []
        for i in range(60):
            observations.append(experiment.HoldoutObservation(
                "L", f"w{i}", False, i % 100 < 50))
        for i in range(60):
            observations.append(experiment.HoldoutObservation(
                "L", f"i{i}", True, i % 100 < 70))
        [fixed] = experiment.analyze(observations)
        [sequential] = experiment.analyze(observations, sequential=True)
        if fixed.verdict == experiment.VERDICT_HELPS and sequential.verdict != (
            experiment.VERDICT_HELPS
        ):
            assert sequential.verdict == experiment.VERDICT_UNDERPOWERED
            assert "running experiment" in sequential.note
