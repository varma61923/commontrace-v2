from __future__ import annotations

import random

import pytest

from commontrace import experiment as ex
from commontrace import integrity, value


def effect(slug, verdict, n_injected, size, lo, hi):
    return ex.CausalEffect(
        lesson_slug=slug, n_injected=n_injected, n_withheld=200,
        rate_injected=0.7, rate_withheld=0.7 - size, effect=size,
        ci_low=lo, ci_high=hi, p_value=0.01,
        significant=verdict in (ex.VERDICT_HELPS, ex.VERDICT_HURTS),
        min_detectable_effect=0.05, verdict=verdict, note="",
    )


def sound() -> integrity.IntegrityReport:
    return integrity.audit([
        integrity.Assignment("l", f"o{i}", i % 2 == 0, 0.5, "s", i % 3 == 0, None, "rev")
        for i in range(60)
    ])


def compromised() -> integrity.IntegrityReport:
    rows = []
    for i in range(600):
        injected = i % 2 == 0
        succeeded = i % 3 == 0
        reported = injected or i % 4 == 0
        rows.append(integrity.Assignment("l", f"o{i}", injected, 0.5, "s",
                                         succeeded if reported else None, None, "rev"))
    return integrity.audit(rows)


class TestTheQuantity:
    def test_it_is_effect_times_the_occasions_that_received_it(self):
        report = value.compute([effect("a", ex.VERDICT_HELPS, 1000, 0.10, 0.05, 0.15)], sound())
        assert report.occasions_improved == pytest.approx(100.0)

    def test_the_interval_carries_through(self):
        report = value.compute([effect("a", ex.VERDICT_HELPS, 1000, 0.10, 0.05, 0.15)], sound())
        assert (report.ci_low, report.ci_high) == pytest.approx((50.0, 150.0))

    def test_it_is_a_count_not_money_until_a_rate_is_supplied(self):
        report = value.compute([effect("a", ex.VERDICT_HELPS, 1000, 0.10, 0.05, 0.15)], sound())
        assert report.money is None and report.money_range is None
        assert "occasions" in value.render(report)

    def test_a_supplied_rate_produces_money_with_a_range(self):
        report = value.compute([effect("a", ex.VERDICT_HELPS, 1000, 0.10, 0.05, 0.15)],
                               sound(), value_per_occasion=20.0)
        assert report.money == pytest.approx(2000.0)
        assert report.money_range == pytest.approx((1000.0, 3000.0))


class TestTheThreeRules:
    def test_a_compromised_experiment_yields_no_figure_at_all(self):
        audit = compromised()
        assert audit.verdict == integrity.VERDICT_COMPROMISED

        report = value.compute([effect("a", ex.VERDICT_HELPS, 1000, 0.10, 0.05, 0.15)],
                               audit, value_per_occasion=20.0)
        assert not report.readable
        assert report.occasions_improved == 0.0
        assert report.money is None
        assert "COMPROMISED" in report.reason
        rendered = value.render(report)
        assert "Not stated" in rendered
        assert "2,000" not in rendered and "20,000" not in rendered

    def test_an_underpowered_memory_contributes_nothing(self):
        report = value.compute([
            effect("real", ex.VERDICT_HELPS, 1000, 0.10, 0.05, 0.15),
            effect("phantom", ex.VERDICT_UNDERPOWERED, 90, 0.30, -0.10, 0.70),
        ], sound())
        assert report.occasions_improved == pytest.approx(100.0)
        assert report.n_counted == 1 and report.n_excluded == 1
        phantom = next(m for m in report.memories if m.slug == "phantom")
        assert not phantom.counted and "not established" in phantom.why_not

    def test_when_nothing_is_counted_the_top_level_reason_names_the_best_trend(self):
        report = value.compute([
            effect("weak", ex.VERDICT_UNDERPOWERED, 5, 0.05, -0.20, 0.30),
            effect("strong", ex.VERDICT_UNDERPOWERED, 11, 0.62, -0.05, 1.00),
        ], sound())
        assert report.readable
        assert report.n_counted == 0
        assert report.occasions_improved == 0.0
        assert "strong" in report.reason
        assert "62" in report.reason
        assert "not enough evidence" in report.reason.lower()
        assert report.reason in value.render(report)

    def test_a_fully_established_run_gets_no_extra_reason_text(self):
        report = value.compute([effect("a", ex.VERDICT_HELPS, 1000, 0.10, 0.05, 0.15)], sound())
        assert report.reason == ""

    def test_memories_that_hurt_are_subtracted_not_dropped(self):
        report = value.compute([
            effect("helps", ex.VERDICT_HELPS, 1000, 0.10, 0.05, 0.15),
            effect("hurts", ex.VERDICT_HURTS, 500, -0.08, -0.14, -0.02),
        ], sound())
        assert report.occasions_improved == pytest.approx(60.0)
        assert report.n_counted == 2
        hurts = next(m for m in report.memories if m.slug == "hurts")
        assert hurts.counted and hurts.occasions_improved == pytest.approx(-40.0)

    def test_the_report_says_out_loud_that_it_subtracted_them(self):
        rendered = value.render(value.compute([
            effect("helps", ex.VERDICT_HELPS, 1000, 0.10, 0.05, 0.15),
            effect("hurts", ex.VERDICT_HURTS, 500, -0.08, -0.14, -0.02),
        ], sound()))
        assert "subtracted above, not dropped" in rendered
        assert "brochure" in rendered

    def test_a_net_negative_result_is_reported_as_negative(self):
        report = value.compute([
            effect("hurts", ex.VERDICT_HURTS, 1000, -0.12, -0.18, -0.06),
        ], sound(), value_per_occasion=50.0)
        assert report.occasions_improved < 0
        assert report.money < 0
        assert "-120" in value.render(report)


class TestAnUnauditedRunIsNotACleanOne:
    def test_no_audit_means_no_figure(self):
        report = value.compute([effect("a", ex.VERDICT_HELPS, 1000, 0.10, 0.05, 0.15)], None)
        assert not report.readable
        assert "not been checked" in report.reason

    def test_a_weakened_run_still_produces_a_figure(self):
        rng = random.Random(4)
        rows = [integrity.Assignment("l", f"o{i}", i % 2 == 0, 0.5, "s",
                                     (i % 3 == 0) if rng.random() > 0.45 else None,
                                     None, "rev")
                for i in range(600)]
        audit = integrity.audit(rows)
        assert audit.verdict == integrity.VERDICT_WEAKENED
        report = value.compute([effect("a", ex.VERDICT_HELPS, 1000, 0.10, 0.05, 0.15)], audit)
        assert report.readable


class TestEdges:
    def test_no_effects_is_zero_rather_than_an_error(self):
        report = value.compute([], sound())
        assert report.readable and report.occasions_improved == 0.0
        assert value.render(report)

    def test_a_memory_never_injected_contributes_nothing(self):
        report = value.compute([effect("a", ex.VERDICT_HELPS, 0, 0.10, 0.05, 0.15)], sound())
        assert report.occasions_improved == 0.0

    def test_a_measured_null_contributes_zero_and_says_that_is_a_result(self):
        report = value.compute([effect("a", ex.VERDICT_NO_EFFECT, 1000, 0.004, -0.02, 0.03)],
                               sound())
        assert report.occasions_improved == 0.0
        item = report.memories[0]
        assert not item.counted
        assert "a result rather than an omission" in item.why_not
