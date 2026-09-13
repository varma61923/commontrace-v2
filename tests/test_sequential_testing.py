"""Looking at a running experiment without manufacturing a result.

Every surface in this product reads a LIVE experiment: `experiment_status`,
the console Proof page, `causal_effects` on every call, `working_set`
promoting a trace the moment it clears significance. That is the product
working as designed, and it is also repeated significance testing on
accumulating data -- which crosses a fixed threshold by luck sooner or
later. Measured here, in a world where the memory does nothing at all, the
fixed threshold declares HELPS or HURTS in roughly a quarter of runs.

That verdict promotes a memory into every future retrieval and feeds an
invoice, so these tests are about the boundary that survives being watched,
and about the two things such a boundary can get wrong: failing to control
the error it exists to control, and being so wide that a real effect never
lands.
"""
from __future__ import annotations

import random

import pytest

from commontrace import experiment


def _run(seed: int, sequential: bool, lift: float, max_n: int = 1000,
         baseline: float = 0.6, step: int = 50) -> int | None:
    """Accrue occasions in blocks, analysing after each block -- the shape of
    a fleet that is watched. Returns the n at which a verdict was declared,
    or None if none was."""
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
        """The property that makes this a redistribution of alpha rather than
        a tax on it: at full information the final look spends what is left."""
        for shape in experiment.SPENDING_FUNCTIONS:
            assert experiment.alpha_spent(1.0, 0.05, shape) == pytest.approx(0.05)

    def test_obrien_fleming_is_miserly_early(self):
        """The behaviour this product needs: an early, noisy, enormous-looking
        effect is exactly what must not promote a memory into every future
        retrieval."""
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
        """"Nothing can be declared significant yet" is the correct reading of
        a look before any data, not "spend a little anyway"."""
        assert experiment.alpha_spent(0.0) == 0.0

    def test_an_unknown_shape_is_refused(self):
        with pytest.raises(ValueError, match="unknown alpha-spending shape"):
            experiment.alpha_spent(0.5, shape="made-up")


class TestTheConfidenceSequence:
    def test_it_is_wider_than_a_fixed_n_interval_at_the_same_n(self):
        """The honest price of being allowed to look whenever you want. If it
        were not wider, it would not be buying anything."""
        fixed = experiment.diff_confidence_interval(60, 100, 45, 100)
        anytime = experiment.anytime_confidence_interval(60, 100, 45, 100)
        assert (anytime[1] - anytime[0]) > (fixed[1] - fixed[0])

    def test_it_narrows_as_evidence_accrues(self):
        wide = experiment.anytime_confidence_interval(30, 50, 20, 50)
        narrow = experiment.anytime_confidence_interval(600, 1000, 400, 1000)
        assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])

    def test_a_degenerate_arm_yields_no_claim(self):
        """Every occasion succeeded in both arms: there is no spread to bound,
        and a zero-width interval would claim certainty from a sample that has
        simply not seen both outcomes."""
        lo, hi = experiment.anytime_confidence_interval(50, 50, 50, 50)
        assert lo <= 0.0 <= hi

    def test_an_empty_arm_yields_no_claim(self):
        lo, hi = experiment.anytime_confidence_interval(0, 0, 10, 20)
        assert lo <= 0.0 <= hi

    def test_an_overwhelming_effect_still_excludes_zero(self):
        """The calibration failure to avoid in the other direction: a
        boundary so wide that 60 per arm with a 50-point effect reads as
        'cannot say' is a miscalibrated instrument, not a careful one."""
        lo, hi = experiment.anytime_confidence_interval(
            48, 60, 18, 60, target_n_per_arm=98
        )
        assert lo > 0.0


class TestWhatContinuousMonitoringDoesToAFixedThreshold:
    def test_a_fixed_threshold_manufactures_results_when_watched(self):
        """The defect, demonstrated rather than asserted: the memory does
        nothing, and peeking as data accrues declares that it does."""
        assert _false_positive_rate(sequential=False) > 0.15

    def test_the_sequential_boundary_holds_the_error(self):
        assert _false_positive_rate(sequential=True) <= 0.05

    def test_the_sequential_boundary_is_strictly_better_here(self):
        assert _false_positive_rate(sequential=True) < _false_positive_rate(
            sequential=False
        )


class TestItStillDetectsRealEffects:
    """A boundary that never fires controls the error rate perfectly and is
    useless. These pin the other side."""

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
        """The trade is detection SPEED, not detection. Both find a large
        effect; the sequence needs more occasions to do it."""
        fixed = [n for n in (_run(s, False, 0.25) for s in range(30)) if n]
        sequential = [n for n in (_run(s, True, 0.25) for s in range(30)) if n]
        assert len(fixed) == len(sequential) == 30
        assert sum(sequential) / 30 > sum(fixed) / 30


class TestTheDefaultIsUnchanged:
    def test_a_one_shot_analysis_is_not_penalised(self):
        """A caller analysing a FINISHED run is not peeking, and paying a
        sequential penalty there would cost power for no gain -- so the
        default has to stay where it was."""
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
        """It must not read as NO_MEASURABLE_EFFECT: that is evidence of
        absence, and this is absence of (sufficient) evidence."""
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
