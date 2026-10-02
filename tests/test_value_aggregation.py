from __future__ import annotations

import math

import pytest

from commontrace import experiment, integrity, value


def _effect(slug, verdict, effect, n_injected=200, half_width=0.02):
    return experiment.CausalEffect(
        lesson_slug=slug, n_injected=n_injected, n_withheld=n_injected,
        rate_injected=0.8, rate_withheld=0.8 - effect, effect=effect,
        ci_low=effect - half_width, ci_high=effect + half_width, p_value=0.001,
        significant=verdict in (experiment.VERDICT_HELPS, experiment.VERDICT_HURTS),
        min_detectable_effect=0.02, verdict=verdict,
    )


def _clean_audit():
    return integrity.audit([
        integrity.Assignment(
            lesson="L", occasion_id=f"o{i}", injected=i % 2 == 0,
            rate=0.5, succeeded=(i % 3 != 0),
        )
        for i in range(400)
    ])


def _assignments(pairs):
    return [
        integrity.Assignment(lesson=lesson, occasion_id=occasion, injected=True)
        for lesson, occasion in pairs
    ]


class TestOverlapDetection:
    def test_memories_on_separate_occasions_do_not_conflict(self):
        overlap = value.overlap_from_assignments(_assignments([
            ("a", "o1"), ("a", "o2"), ("b", "o3"), ("b", "o4"),
        ]))
        assert overlap.shared_pairs == frozenset()
        assert overlap.unique_injected_occasions == 4
        assert overlap.conflicts_among(["a", "b"]) == []

    def test_one_shared_occasion_makes_a_pair(self):
        overlap = value.overlap_from_assignments(_assignments([
            ("a", "o1"), ("b", "o1"), ("b", "o2"),
        ]))
        assert overlap.conflicts_among(["a", "b"]) == [("a", "b")]
        assert overlap.unique_injected_occasions == 2

    def test_a_withheld_row_cannot_double_attribute(self):
        rows = [
            integrity.Assignment(lesson="a", occasion_id="o1", injected=True),
            integrity.Assignment(lesson="b", occasion_id="o1", injected=False),
        ]
        assert value.overlap_from_assignments(rows).shared_pairs == frozenset()

    def test_a_conflict_with_an_uncounted_memory_is_not_a_conflict(self):
        overlap = value.overlap_from_assignments(_assignments([
            ("a", "o1"), ("underpowered", "o1"),
        ]))
        assert overlap.conflicts_among(["a"]) == []
        assert overlap.conflicts_among(["a", "underpowered"]) == [("a", "underpowered")]


class TestTheAggregateRefusesToDoubleAttribute:
    def test_overlapping_counted_memories_yield_no_total(self):
        overlap = value.overlap_from_assignments(_assignments([
            ("a", "shared"), ("b", "shared"),
        ]))
        report = value.compute(
            [
                _effect("a", experiment.VERDICT_HELPS, 0.05),
                _effect("b", experiment.VERDICT_HELPS, 0.04),
            ],
            _clean_audit(), value_per_occasion=25.0, overlap=overlap,
        )
        assert report.readable
        assert len(report.memories) == 2
        assert not report.aggregate_readable
        assert "overlapping occasions" in report.aggregate_reason
        assert report.money is None
        assert report.ledger() == []

    def test_disjoint_counted_memories_still_produce_a_total(self):
        overlap = value.overlap_from_assignments(_assignments([
            ("a", "o1"), ("b", "o2"),
        ]))
        report = value.compute(
            [
                _effect("a", experiment.VERDICT_HELPS, 0.05),
                _effect("b", experiment.VERDICT_HELPS, 0.04),
            ],
            _clean_audit(), value_per_occasion=25.0, overlap=overlap,
        )
        assert report.aggregate_readable
        assert report.money == pytest.approx(report.occasions_improved * 25.0)

    def test_an_unknown_overlap_is_not_treated_as_no_overlap(self):
        report = value.compute(
            [
                _effect("a", experiment.VERDICT_HELPS, 0.05),
                _effect("b", experiment.VERDICT_HELPS, 0.04),
            ],
            _clean_audit(), value_per_occasion=25.0,
        )
        assert not report.aggregate_readable
        assert "not known" in report.aggregate_reason
        assert report.money is None

    def test_a_single_memory_needs_no_overlap_record(self):
        report = value.compute(
            [_effect("only", experiment.VERDICT_HELPS, 0.05)],
            _clean_audit(), value_per_occasion=25.0,
        )
        assert report.aggregate_readable
        assert report.money is not None

    def test_overlap_between_an_uncounted_pair_does_not_block_the_total(self):
        overlap = value.overlap_from_assignments(_assignments([
            ("a", "shared"), ("weak", "shared"), ("b", "o2"),
        ]))
        report = value.compute(
            [
                _effect("a", experiment.VERDICT_HELPS, 0.05),
                _effect("b", experiment.VERDICT_HELPS, 0.04),
                _effect("weak", experiment.VERDICT_UNDERPOWERED, 0.20),
            ],
            _clean_audit(), value_per_occasion=25.0, overlap=overlap,
        )
        assert report.aggregate_readable, report.aggregate_reason


class TestTheJointInterval:
    def test_the_interval_is_combined_in_quadrature_not_by_summing_endpoints(self):
        overlap = value.overlap_from_assignments(_assignments([
            ("a", "o1"), ("b", "o2"),
        ]))
        report = value.compute(
            [
                _effect("a", experiment.VERDICT_HELPS, 0.05, n_injected=100,
                        half_width=0.02),
                _effect("b", experiment.VERDICT_HELPS, 0.05, n_injected=100,
                        half_width=0.02),
            ],
            _clean_audit(), overlap=overlap,
        )
        per_memory_half_width = 0.02 * 100
        summed_endpoints = 2 * per_memory_half_width
        quadrature = math.sqrt(2) * per_memory_half_width

        actual_half_width = (report.ci_high - report.ci_low) / 2
        assert actual_half_width == pytest.approx(quadrature, rel=1e-3)
        assert actual_half_width < summed_endpoints

    def test_the_interval_still_straddles_the_estimate(self):
        report = value.compute(
            [_effect("a", experiment.VERDICT_HELPS, 0.05)], _clean_audit()
        )
        assert report.ci_low < report.occasions_improved < report.ci_high

    def test_one_memory_keeps_its_own_interval(self):
        report = value.compute(
            [_effect("a", experiment.VERDICT_HELPS, 0.05, n_injected=100,
                     half_width=0.02)],
            _clean_audit(),
        )
        [memory] = report.memories
        assert report.ci_low == pytest.approx(memory.ci_low, abs=0.01)
        assert report.ci_high == pytest.approx(memory.ci_high, abs=0.01)


class TestPostSelectionIsReportedAsANumber:
    def test_the_unselected_total_includes_measured_non_significant_effects(self):
        report = value.compute(
            [
                _effect("won", experiment.VERDICT_HELPS, 0.05, n_injected=100),
                _effect("null", experiment.VERDICT_NO_EFFECT, -0.01, n_injected=100),
            ],
            _clean_audit(), overlap=value.overlap_from_assignments(_assignments([
                ("won", "o1"), ("null", "o2"),
            ])),
        )
        assert report.occasions_improved == pytest.approx(5.0)
        assert report.occasions_improved_unselected == pytest.approx(4.0)
        assert report.n_examined == 2
        assert report.n_counted == 1

    def test_underpowered_memories_are_in_neither_total(self):
        report = value.compute(
            [
                _effect("won", experiment.VERDICT_HELPS, 0.05, n_injected=100),
                _effect("weak", experiment.VERDICT_UNDERPOWERED, 0.50, n_injected=100),
            ],
            _clean_audit(), overlap=value.overlap_from_assignments(_assignments([
                ("won", "o1"), ("weak", "o2"),
            ])),
        )
        assert report.occasions_improved == pytest.approx(5.0)
        assert report.occasions_improved_unselected == pytest.approx(5.0)

    def test_the_unselected_total_is_never_used_as_money(self):
        report = value.compute(
            [
                _effect("won", experiment.VERDICT_HELPS, 0.05, n_injected=100),
                _effect("null", experiment.VERDICT_NO_EFFECT, 0.30, n_injected=100),
            ],
            _clean_audit(), value_per_occasion=10.0,
            overlap=value.overlap_from_assignments(_assignments([
                ("won", "o1"), ("null", "o2"),
            ])),
        )
        assert report.money == pytest.approx(report.occasions_improved * 10.0)
        assert report.money != pytest.approx(
            report.occasions_improved_unselected * 10.0
        )


class TestThePolicyLevelAggregate:
    @staticmethod
    def _fleet(n=400, lift=0.2, seed=3):
        import random

        rng = random.Random(seed)
        rows = []
        for i in range(n):
            occasion = f"o{i}"
            injected = {"a": rng.random() > 0.5, "b": rng.random() > 0.5}
            treated = any(injected.values())
            succeeded = rng.random() < (0.5 + (lift if treated else 0.0))
            for lesson, inject in injected.items():
                rows.append(integrity.Assignment(
                    lesson=lesson, occasion_id=occasion,
                    injected=inject, succeeded=succeeded,
                ))
        return rows

    def test_each_occasion_is_counted_exactly_once(self):
        rows = self._fleet(n=200)
        effect = value.policy_effect(rows)
        assert effect.n_treated + effect.n_control == 200

    def test_it_recovers_a_real_lift(self):
        effect = value.policy_effect(self._fleet(n=3000, lift=0.2))
        assert effect.readable
        assert effect.ci_low <= 0.2 <= effect.ci_high
        assert effect.significant

    def test_it_finds_nothing_when_there_is_nothing(self):
        effect = value.policy_effect(self._fleet(n=3000, lift=0.0))
        assert effect.readable
        assert not effect.significant
        assert effect.ci_low <= 0.0 <= effect.ci_high

    def test_a_thin_control_arm_is_refused_rather_than_reported(self):
        rows = [
            integrity.Assignment(lesson="a", occasion_id=f"o{i}", injected=True,
                                 succeeded=True)
            for i in range(50)
        ]
        effect = value.policy_effect(rows)
        assert not effect.readable
        assert "Not enough resolved occasions" in effect.reason
        assert effect.n_control == 0

    def test_unresolved_occasions_are_in_neither_arm(self):
        rows = [
            integrity.Assignment(lesson="a", occasion_id="resolved", injected=True,
                                 succeeded=True),
            integrity.Assignment(lesson="a", occasion_id="pending", injected=True,
                                 succeeded=None),
        ]
        effect = value.policy_effect(rows)
        assert effect.n_treated + effect.n_control == 1

    def test_compute_derives_the_policy_effect_from_assignments(self):
        rows = self._fleet(n=3000, lift=0.2)
        report = value.compute(
            [_effect("a", experiment.VERDICT_HELPS, 0.05)],
            _clean_audit(), assignments=rows,
        )
        assert report.policy is not None
        assert report.policy.readable
        expected_injected_occasions = len({r.occasion_id for r in rows if r.injected})
        assert report.unique_occasions == expected_injected_occasions
        assert report.policy.n_treated == expected_injected_occasions

    def test_the_policy_figure_survives_an_overlap_that_blocks_the_sum(self):
        rows = self._fleet(n=3000, lift=0.2)
        report = value.compute(
            [
                _effect("a", experiment.VERDICT_HELPS, 0.05),
                _effect("b", experiment.VERDICT_HELPS, 0.04),
            ],
            _clean_audit(), value_per_occasion=25.0, assignments=rows,
        )
        assert not report.aggregate_readable
        assert report.money is None
        assert report.policy is not None and report.policy.readable
        assert report.policy.occasions_improved > 0


class TestWhatGetsRendered:
    def test_no_headline_total_when_the_memories_may_not_be_added(self):
        overlap = value.overlap_from_assignments(_assignments([
            ("a", "shared"), ("b", "shared"),
        ]))
        report = value.compute(
            [
                _effect("a", experiment.VERDICT_HELPS, 0.05),
                _effect("b", experiment.VERDICT_HELPS, 0.04),
            ],
            _clean_audit(), value_per_occasion=25.0, overlap=overlap,
        )
        rendered = value.render(report)
        assert "No total is stated" in rendered
        assert "went differently" not in rendered
        assert "per resolved occasion you supplied" not in rendered
        assert "`a`" in rendered and "`b`" in rendered

    def test_the_policy_figure_is_offered_in_its_place(self):
        rows = TestThePolicyLevelAggregate._fleet(n=3000, lift=0.2)
        report = value.compute(
            [
                _effect("a", experiment.VERDICT_HELPS, 0.05),
                _effect("b", experiment.VERDICT_HELPS, 0.04),
            ],
            _clean_audit(), assignments=rows,
        )
        rendered = value.render(report)
        assert "Whole-policy comparison" in rendered
        assert "counts once" in rendered

    def test_a_total_is_still_rendered_when_it_is_valid(self):
        report = value.compute(
            [_effect("a", experiment.VERDICT_HELPS, 0.05)],
            _clean_audit(), value_per_occasion=25.0,
        )
        rendered = value.render(report)
        assert "went differently" in rendered
        assert "No total is stated" not in rendered

    def test_the_unselected_total_is_shown_beside_the_billable_one(self):
        report = value.compute(
            [
                _effect("won", experiment.VERDICT_HELPS, 0.05, n_injected=100),
                _effect("null", experiment.VERDICT_NO_EFFECT, 0.02, n_injected=100),
            ],
            _clean_audit(),
            overlap=value.OccasionOverlap(frozenset(), 200),
        )
        rendered = value.render(report)
        assert "Counting every measured memory" in rendered
        assert "not billable" in rendered


class TestTheHonestDenominator:
    def test_unique_occasions_counts_distinct_occasions_not_injections(self):
        overlap = value.overlap_from_assignments(_assignments([
            ("a", "o1"), ("b", "o1"), ("b", "o2"),
        ]))
        report = value.compute(
            [_effect("a", experiment.VERDICT_HELPS, 0.05)],
            _clean_audit(), overlap=overlap,
        )
        assert report.unique_occasions == 2

    def test_it_is_none_when_no_assignment_record_was_given(self):
        report = value.compute(
            [_effect("a", experiment.VERDICT_HELPS, 0.05)], _clean_audit()
        )
        assert report.unique_occasions is None
