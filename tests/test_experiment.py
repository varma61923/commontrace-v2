"""Tests for commontrace/experiment.py -- the randomized-holdout causal layer.

The property under test is not "the arithmetic is right" but that the
module answers a question observation cannot: whether injecting a lesson
*causes* better outcomes. The central test here
(TestCorrelationVsCausation) constructs a world with a known ground truth
where correlational scoring gives the exactly-backwards answer, and asserts
the holdout recovers the truth. If that test ever passes vacuously, the
module has no reason to exist.
"""
import json
import math
import os

import pytest
import yaml

from commontrace import experiment as ex
from commontrace.cli import main


class TestHoldoutAssignment:
    def test_is_deterministic(self):
        """A disputed result must be reproducible months later, and a retried
        occasion must not silently change arms mid-experiment."""
        first = [ex.is_held_out("lesson_a", f"occ{i}") for i in range(200)]
        second = [ex.is_held_out("lesson_a", f"occ{i}") for i in range(200)]
        assert first == second

    def test_rate_zero_never_withholds(self):
        assert not any(ex.is_held_out("l", f"o{i}", rate=0.0) for i in range(100))

    def test_rate_one_always_withholds(self):
        assert all(ex.is_held_out("l", f"o{i}", rate=1.0) for i in range(100))

    def test_negative_rate_is_treated_as_off_not_as_an_error(self):
        assert not ex.is_held_out("l", "o", rate=-0.5)

    @pytest.mark.parametrize("rate", [0.10, 0.25, 0.50])
    def test_observed_rate_matches_the_requested_rate(self, rate):
        n = 20_000
        held = sum(ex.is_held_out("lesson_a", f"occ{i}", rate=rate) for i in range(n))
        # 4 standard errors of a binomial proportion -- wide enough never to
        # flake, tight enough to catch a genuinely skewed hash.
        se = math.sqrt(rate * (1 - rate) / n)
        assert abs(held / n - rate) < 4 * se

    def test_assignment_is_independent_across_lessons(self):
        """The property that makes always-co-firing lessons separable.

        Two lessons retrieved together on every occasion share every outcome,
        so no observational method can attribute the outcome to one of them.
        Independent holdout draws break the tie by construction: some
        occasions get A without B, and vice versa. If the draws were
        correlated -- e.g. if the hash ignored the slug -- that would collapse
        and the whole approach would fail silently.
        """
        n = 20_000
        both = sum(
            ex.is_held_out("lesson_a", f"occ{i}", rate=0.5)
            and ex.is_held_out("lesson_b", f"occ{i}", rate=0.5)
            for i in range(n)
        )
        se = math.sqrt(0.25 * 0.75 / n)
        assert abs(both / n - 0.25) < 4 * se

    def test_changing_the_salt_reshuffles_the_experiment(self):
        a = [ex.is_held_out("l", f"o{i}", rate=0.5, salt="expt-1") for i in range(500)]
        b = [ex.is_held_out("l", f"o{i}", rate=0.5, salt="expt-2") for i in range(500)]
        assert a != b


class TestTwoProportionTest:
    def test_identical_arms_are_not_significant(self):
        _, p = ex.two_proportion_test(50, 100, 50, 100)
        assert p == pytest.approx(1.0)

    def test_matches_the_closed_form_on_a_known_case(self):
        """Guards against a silent change of standard-error form. Computed by
        hand for 80/100 vs 60/100: p_pool=0.70, se=sqrt(.7*.3*(1/100+1/100))."""
        z, p = ex.two_proportion_test(80, 100, 60, 100)
        se = math.sqrt(0.7 * 0.3 * 0.02)
        assert z == pytest.approx(0.20 / se)
        assert p < 0.01

    def test_large_difference_with_large_n_is_significant(self):
        _, p = ex.two_proportion_test(90, 100, 50, 100)
        assert p < 0.001

    def test_same_difference_with_tiny_n_is_not_significant(self):
        """The reason raw rate differences are never reported alone: 90% vs
        50% is the same headline number at n=10 and at n=100, and only one of
        them is evidence."""
        _, p = ex.two_proportion_test(9, 10, 5, 10)
        assert p > 0.05

    def test_empty_arm_does_not_raise(self):
        assert ex.two_proportion_test(0, 0, 5, 10) == (0.0, 1.0)

    def test_no_variance_in_either_arm_reports_nothing_to_detect(self):
        assert ex.two_proportion_test(10, 10, 10, 10) == (0.0, 1.0)
        assert ex.two_proportion_test(0, 10, 0, 10) == (0.0, 1.0)


class TestConfidenceInterval:
    def test_contains_the_point_estimate(self):
        lo, hi = ex.diff_confidence_interval(80, 100, 60, 100)
        assert lo < 0.20 < hi

    def test_narrows_as_evidence_grows(self):
        small = ex.diff_confidence_interval(8, 10, 6, 10)
        large = ex.diff_confidence_interval(800, 1000, 600, 1000)
        assert (large[1] - large[0]) < (small[1] - small[0])

    def test_a_significant_effect_has_an_interval_excluding_zero(self):
        """The interval and the p-value must tell the same story; they use
        different standard errors and can be made to disagree by mixing the
        pooled and unpooled forms up."""
        _, p = ex.two_proportion_test(90, 100, 50, 100)
        lo, hi = ex.diff_confidence_interval(90, 100, 50, 100)
        assert p < 0.05 and lo > 0

    def test_empty_arm_does_not_raise(self):
        assert ex.diff_confidence_interval(0, 0, 0, 0) == (0.0, 0.0)


class TestBenjaminiHochberg:
    def test_empty_input(self):
        assert ex.benjamini_hochberg([]) == []

    def test_obviously_significant_values_survive(self):
        assert ex.benjamini_hochberg([0.0001, 0.0002]) == [True, True]

    def test_obviously_null_values_do_not(self):
        assert ex.benjamini_hochberg([0.4, 0.6, 0.9]) == [False, False, False]

    def test_a_lone_borderline_value_is_rejected_among_many_nulls(self):
        """The multiple-comparison problem itself. At alpha=0.05 across 100
        lessons, ~5 will look significant by chance -- and those are exactly
        the ones a report would quote."""
        p_values = [0.04] + [0.5] * 99
        assert not any(ex.benjamini_hochberg(p_values))

    def test_is_less_conservative_than_bonferroni(self):
        """Why BH and not Bonferroni: on a corpus of this shape Bonferroni
        would discard real effects."""
        p_values = [0.001, 0.002, 0.003, 0.004] + [0.5] * 16
        kept = ex.benjamini_hochberg(p_values)
        bonferroni = [p <= 0.05 / len(p_values) for p in p_values]
        assert sum(kept) > sum(bonferroni)

    def test_result_order_matches_input_order(self):
        """Values arrive sorted by lesson, not by p. Returning the mask in
        rank order instead would mislabel every lesson."""
        assert ex.benjamini_hochberg([0.9, 0.0001, 0.9]) == [False, True, False]

    def test_step_up_keeps_a_p_that_fails_its_own_threshold(self):
        """BH is a step-*up* procedure: once the largest surviving rank is
        found, every smaller-p hypothesis is kept too, even one that fails
        its own threshold. Testing each p against its own rank independently
        is the classic misimplementation, and it is strictly less powerful.
        """
        # m=3, alpha=0.05 -> thresholds 0.0167, 0.0333, 0.05.
        # 0.034 exceeds its own rank-2 threshold, but rank 3 survives, so BH
        # steps up and keeps everything at or below it.
        p_values = [0.001, 0.034, 0.045]
        assert ex.benjamini_hochberg(p_values) == [True, True, True]


class TestMinimumDetectableEffect:
    def test_shrinks_as_the_sample_grows(self):
        assert ex.minimum_detectable_effect(1000, 0.5) < ex.minimum_detectable_effect(10, 0.5)

    def test_tiny_samples_can_only_detect_enormous_effects(self):
        """Quoted alongside every 'no effect' verdict so it reads as 'cannot
        answer yet' rather than 'proven ineffective'."""
        assert ex.minimum_detectable_effect(10, 0.5) > 0.40

    def test_degenerate_inputs_return_none_rather_than_a_number(self):
        assert ex.minimum_detectable_effect(0, 0.5) is None
        assert ex.minimum_detectable_effect(100, 0.0) is None
        assert ex.minimum_detectable_effect(100, 1.0) is None


def _obs(slug, n, injected, success_rate, offset=0):
    """n occasions for `slug` in one arm, `success_rate` of them successful."""
    n_success = round(n * success_rate)
    return [
        ex.HoldoutObservation(
            lesson_slug=slug,
            occasion_id=f"{slug}-{'i' if injected else 'w'}-{offset + i}",
            injected=injected,
            succeeded=i < n_success,
        )
        for i in range(n)
    ]


class TestAnalyze:
    def test_a_lesson_that_clearly_helps_is_reported_as_helps(self):
        obs = _obs("l", 100, True, 0.90) + _obs("l", 100, False, 0.50)
        (effect,) = ex.analyze(obs)
        assert effect.verdict == ex.VERDICT_HELPS
        assert effect.effect == pytest.approx(0.40)
        assert effect.significant and effect.ci_low > 0

    def test_a_lesson_that_clearly_hurts_is_reported_as_hurts(self):
        """The verdict correlational scoring structurally cannot produce."""
        obs = _obs("l", 100, True, 0.40) + _obs("l", 100, False, 0.85)
        (effect,) = ex.analyze(obs)
        assert effect.verdict == ex.VERDICT_HURTS
        assert effect.effect < 0 and effect.ci_high < 0

    def test_no_real_difference_is_reported_as_no_measurable_effect(self):
        obs = _obs("l", 100, True, 0.60) + _obs("l", 100, False, 0.60)
        (effect,) = ex.analyze(obs)
        assert effect.verdict == ex.VERDICT_NO_EFFECT

    def test_no_effect_carries_the_detectable_size_so_it_is_not_read_as_absence(self):
        obs = _obs("l", 30, True, 0.60) + _obs("l", 30, False, 0.60)
        (effect,) = ex.analyze(obs)
        assert effect.verdict == ex.VERDICT_NO_EFFECT
        assert effect.min_detectable_effect is not None
        assert "could only have detected" in effect.note

    def test_a_thin_arm_is_underpowered_not_no_effect(self):
        """A 5-vs-5 comparison is not evidence of anything. Reporting it as
        'no effect' would let a real regression sit unflagged."""
        obs = _obs("l", 50, True, 0.90) + _obs("l", 5, False, 0.20)
        (effect,) = ex.analyze(obs)
        assert effect.verdict == ex.VERDICT_UNDERPOWERED
        assert not effect.significant
        assert "not 'no effect'" in effect.note

    def test_a_lesson_never_withheld_is_underpowered_not_helpful(self):
        """Without a withheld arm there is no comparison at all -- the exact
        situation the whole module exists to escape."""
        (effect,) = ex.analyze(_obs("l", 200, True, 0.95))
        assert effect.verdict == ex.VERDICT_UNDERPOWERED

    def test_underpowered_lessons_do_not_enter_the_fdr_correction(self):
        """Including them would inflate m and weaken every genuine result:
        a real effect could be erased by lessons that were never tested."""
        real = _obs("real", 100, True, 0.85) + _obs("real", 100, False, 0.50)
        thin = []
        for i in range(40):
            thin += _obs(f"thin{i}", 4, True, 0.5) + _obs(f"thin{i}", 4, False, 0.5)

        alone = {e.lesson_slug: e for e in ex.analyze(real)}
        crowded = {e.lesson_slug: e for e in ex.analyze(real + thin)}
        assert alone["real"].verdict == ex.VERDICT_HELPS
        assert crowded["real"].verdict == ex.VERDICT_HELPS
        assert crowded["real"].p_value == alone["real"].p_value

    def test_harmful_lessons_are_listed_first(self):
        obs = _obs("helps", 100, True, 0.90) + _obs("helps", 100, False, 0.50)
        obs += _obs("hurts", 100, True, 0.40) + _obs("hurts", 100, False, 0.85)
        obs += _obs("flat", 100, True, 0.60) + _obs("flat", 100, False, 0.60)
        assert [e.lesson_slug for e in ex.analyze(obs)] == ["hurts", "helps", "flat"]

    def test_empty_input_returns_nothing_rather_than_raising(self):
        assert ex.analyze([]) == []

    def test_min_arm_is_configurable(self):
        obs = _obs("l", 6, True, 1.0) + _obs("l", 6, False, 0.0)
        assert ex.analyze(obs, min_arm=10)[0].verdict == ex.VERDICT_UNDERPOWERED
        assert ex.analyze(obs, min_arm=5)[0].verdict == ex.VERDICT_HELPS


class TestCorrelationVsCausation:
    """The claim the module is built on, stated as an executable test.

    A lesson fires *because* the situation matched it, so the occasions where
    it fired differ systematically from those where it did not. Below, that
    selection effect is strong enough to invert the answer -- and the bias
    does not shrink with n, so no amount of extra observation would rescue
    the correlational read.
    """

    @staticmethod
    def _world(n, true_effect, fires_on_hard_tasks):
        """A fleet where task difficulty drives both retrieval and outcome.

        `fires_on_hard_tasks` is the confound: the lesson is retrieved on the
        gnarly occasions, which fail more often for reasons that have nothing
        to do with the lesson.
        """
        base_easy, base_hard = 0.80, 0.30
        rows, fired, not_fired = [], [], []
        for i in range(n):
            occ = f"occ{i}"
            hard = ex._uniform_from("difficulty", occ) < 0.5
            eligible = hard if fires_on_hard_tasks else not hard
            baseline = base_hard if hard else base_easy
            if not eligible:
                # Occasions the lesson never matched. A correlational read
                # uses these as its comparison group; a causal one must not.
                not_fired.append(ex._uniform_from("outcome", occ) < baseline)
                continue
            withheld = ex.is_held_out("l", occ, rate=0.5)
            rate = baseline if withheld else min(1.0, baseline + true_effect)
            ok = ex._uniform_from("outcome", occ) < rate
            rows.append(ex.HoldoutObservation("l", occ, injected=not withheld, succeeded=ok))
            if not withheld:
                fired.append(ok)
        correlational = (sum(fired) / len(fired)) - (sum(not_fired) / len(not_fired))
        return rows, correlational

    def test_correlational_lift_calls_a_helpful_lesson_harmful(self):
        rows, correlational = self._world(4000, true_effect=+0.20, fires_on_hard_tasks=True)
        assert correlational < 0, "the confound must actually bite for this test to mean anything"

        (effect,) = ex.analyze(rows)
        assert effect.verdict == ex.VERDICT_HELPS
        assert effect.ci_low < 0.20 < effect.ci_high

    def test_correlational_lift_calls_a_useless_lesson_valuable(self):
        rows, correlational = self._world(4000, true_effect=0.0, fires_on_hard_tasks=False)
        assert correlational > 0.20, "the confound must actually bite"

        (effect,) = ex.analyze(rows)
        assert effect.verdict == ex.VERDICT_NO_EFFECT
        assert effect.ci_low < 0 < effect.ci_high

    def test_the_estimate_converges_on_the_true_effect(self):
        rows, _ = self._world(8000, true_effect=+0.20, fires_on_hard_tasks=True)
        (effect,) = ex.analyze(rows)
        assert abs(effect.effect - 0.20) < 0.05


class TestRender:
    def test_renders_with_no_data(self):
        out = ex.render(ex.ExperimentSummary(0, 0, 0.10, []))
        assert "Causal Effect Report" in out

    def test_states_how_the_measurement_was_made(self):
        """A number that drives a purchasing decision must carry its method."""
        out = ex.render(ex.ExperimentSummary(0, 0, 0.10, []))
        assert "withholding" in out and "confounded" in out
        assert "deterministic hash" in out

    def test_a_tiny_p_value_is_never_printed_as_exactly_zero(self):
        """`p = 0.000` claims something impossible and is the first thing a
        technical reviewer notices."""
        obs = _obs("l", 200, True, 0.95) + _obs("l", 200, False, 0.30)
        effects = ex.analyze(obs)
        out = ex.render(ex.ExperimentSummary(len(obs), 1, 0.10, effects))
        assert "<0.001" in out
        assert "| 0.000 |" not in out

    def test_underpowered_lessons_are_separated_from_tested_ones(self):
        obs = _obs("tested", 100, True, 0.90) + _obs("tested", 100, False, 0.50)
        obs += _obs("thin", 3, True, 1.0) + _obs("thin", 3, False, 0.0)
        effects = ex.analyze(obs)
        out = ex.render(ex.ExperimentSummary(len(obs), 2, 0.10, effects))
        assert "Not enough data yet" in out
        assert out.index("Measured effects") < out.index("Not enough data yet")
        assert "have not been tested" in out


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "code", "--dest", str(tmp_path)])
    return tmp_path


def _write_lesson(store, slug, description, tags):
    fm = {
        "name": slug, "description": description, "applies_when": description,
        "tags": tags, "domain": "other", "status": "active", "importance": 3, "uses": 0,
    }
    (store / "memory" / "lessons" / f"lesson_{slug}.md").write_text(
        "---\n" + yaml.safe_dump(fm) + "---\n\nbody\n", encoding="utf-8"
    )


def _write_episode(store, name, verdict, day):
    (store / "memory" / "episodes" / f"2026-01-{day:02d}_{name}.md").write_text(
        "---\n" + yaml.safe_dump({"name": name, "verdict": verdict}) + "---\n\nbody\n",
        encoding="utf-8",
    )


class TestExperimentCLI:
    def test_no_assignments_explains_the_confound_rather_than_printing_zeros(self, store, capsys):
        capsys.readouterr()
        assert main(["experiment", "--dest", str(store)]) == 0
        err = capsys.readouterr().err
        assert "CORRELATIONAL" in err
        assert "--experiment --occasion-id" in err

    def test_query_experiment_requires_an_occasion_id(self, store, capsys):
        _write_lesson(store, "webhook", "retry failed webhook delivery", ["webhooks"])
        capsys.readouterr()
        rc = main(["query", "retry failed webhook", "--lexical", "--experiment", "--dest", str(store)])
        assert rc == 1
        assert "requires --occasion-id" in capsys.readouterr().err

    def test_query_experiment_logs_every_eligible_lesson_with_its_arm(self, store, capsys):
        _write_lesson(store, "webhook", "retry failed webhook delivery", ["webhooks"])
        capsys.readouterr()
        rc = main([
            "query", "retry failed webhook", "--lexical", "--experiment",
            "--occasion-id", "task-1", "--holdout-rate", "1.0", "--dest", str(store),
        ])
        assert rc == 0

        log = store / "memory" / "holdout_log.jsonl"
        records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        assert len(records) == 1
        assert records[0]["occasion_id"] == "task-1"
        assert records[0]["injected"] is False
        assert "[WITHHELD" in capsys.readouterr().out

    def test_assignments_without_outcomes_report_what_is_missing(self, store, capsys):
        _write_lesson(store, "webhook", "retry failed webhook delivery", ["webhooks"])
        main([
            "query", "retry failed webhook", "--lexical", "--experiment",
            "--occasion-id", "task-1", "--dest", str(store),
        ])
        capsys.readouterr()
        assert main(["experiment", "--dest", str(store)]) == 0
        assert "none have a matching" in capsys.readouterr().err

    def test_full_round_trip_from_retrieval_to_causal_verdict(self, store, capsys):
        """query --experiment writes the arms; episodes supply the outcomes;
        experiment joins them. A break anywhere in that chain silently yields
        'no data', so the join is asserted end to end."""
        _write_lesson(store, "webhook", "retry failed webhook delivery", ["webhooks"])

        n = 120
        for i in range(n):
            occ = f"task{i}"
            main([
                "query", "retry failed webhook", "--lexical", "--experiment",
                "--occasion-id", occ, "--holdout-rate", "0.5", "--dest", str(store),
            ])
            withheld = ex.is_held_out("webhook", occ, rate=0.5)
            # Ground truth: injecting the lesson genuinely helps.
            succeeded = ex._uniform_from("outcome", occ) < (0.40 if withheld else 0.90)
            _write_episode(store, occ, "CONFORM" if succeeded else "ABANDON", (i % 28) + 1)

        capsys.readouterr()
        assert main(["experiment", "--dest", str(store), "--json"]) == 0
        data = json.loads(capsys.readouterr().out)

        assert data["n_observations"] == n
        assert data["n_lessons"] == 1
        (effect,) = data["effects"]
        assert effect["lesson_slug"] == "webhook"
        assert effect["verdict"] == ex.VERDICT_HELPS
        assert effect["ci_low"] > 0

    def test_strict_exits_nonzero_when_a_lesson_causes_harm(self, store, capsys, monkeypatch):
        log = store / "memory" / "holdout_log.jsonl"
        lines = []
        for i in range(120):
            occ = f"task{i}"
            withheld = ex.is_held_out("bad", occ, rate=0.5)
            lines.append(json.dumps({
                "occasion_id": occ, "lesson": "bad", "injected": not withheld,
                "rate": 0.5, "salt": "default",
            }))
            succeeded = ex._uniform_from("outcome", occ) < (0.85 if withheld else 0.35)
            _write_episode(store, occ, "CONFORM" if succeeded else "ABANDON", (i % 28) + 1)
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        capsys.readouterr()
        assert main(["experiment", "--dest", str(store), "--strict"]) == 1
        assert "significantly hurt outcomes" in capsys.readouterr().err
        assert main(["experiment", "--dest", str(store)]) == 0

    def test_a_corrupt_log_line_is_skipped_rather_than_aborting_the_report(self, store, capsys):
        log = store / "memory" / "holdout_log.jsonl"
        log.write_text(
            "not json at all\n"
            + json.dumps({"occasion_id": "t1", "lesson": "l", "injected": True, "rate": 0.5})
            + "\n",
            encoding="utf-8",
        )
        _write_episode(store, "t1", "CONFORM", 1)
        capsys.readouterr()
        assert main(["experiment", "--dest", str(store), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["n_observations"] == 1


class TestMinimumDetectableEffectPower:
    """`power` was accepted and ignored -- both branches of a ternary were the
    80% constant -- so asking for 95% power silently returned the 80% answer
    and understated the sample size a real experiment needs."""

    def test_higher_power_demands_a_larger_effect_to_detect(self):
        at80 = ex.minimum_detectable_effect(100, 0.5, power=0.80)
        at90 = ex.minimum_detectable_effect(100, 0.5, power=0.90)
        at95 = ex.minimum_detectable_effect(100, 0.5, power=0.95)
        assert at80 < at90 < at95

    def test_the_default_is_still_eighty_percent(self):
        assert ex.minimum_detectable_effect(100, 0.5) == ex.minimum_detectable_effect(100, 0.5, power=0.80)

    @pytest.mark.parametrize("power,expected_z", [
        (0.80, 0.8416212335729143),
        (0.90, 1.2815515655446004),
        (0.95, 1.6448536269514722),
    ])
    def test_the_normal_quantile_matches_the_standard_value(self, power, expected_z):
        assert ex._z_for_power(power) == pytest.approx(expected_z, abs=1e-6)

    def test_an_impossible_power_is_rejected_rather_than_silently_clamped(self):
        for bad in (0.0, 0.4, 1.0, 1.5):
            with pytest.raises(ValueError, match="power"):
                ex.minimum_detectable_effect(100, 0.5, power=bad)


class TestHoldoutLogDeduplication:
    """The holdout log is append-only, so a retried task writes the same
    (lesson, occasion) pair again. Counting it twice inflates the arm and
    deflates the p-value -- a retry storm would manufacture significance."""

    def test_a_retried_occasion_is_counted_once(self, store, capsys):
        from commontrace.commands.experiment_cmd import holdout_log_path

        main(["init", "--agent-type", "code", "--dest", str(store)])
        log = holdout_log_path(str(store))
        os.makedirs(os.path.dirname(log), exist_ok=True)
        rows = []
        for i in range(40):
            occ = f"task{i}"
            withheld = ex.is_held_out("l", occ, rate=0.5)
            # three identical rows per occasion, as three retries would write
            for _ in range(3):
                rows.append(json.dumps({"occasion_id": occ, "lesson": "l",
                                        "injected": not withheld, "rate": 0.5, "salt": "default"}))
            _write_episode(store, occ, "CONFORM" if ex._uniform_from("o", occ) < 0.6 else "ABANDON",
                           (i % 28) + 1)
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("\n".join(rows) + "\n")

        capsys.readouterr()
        assert main(["experiment", "--dest", str(store), "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        # 40 occasions, not 120 rows
        assert data["n_observations"] == 40
        total_arms = sum(e["n_injected"] + e["n_withheld"] for e in data["effects"])
        assert total_arms == 40

    def test_the_collapse_is_reported_not_silent(self, store, capsys):
        from commontrace.commands.experiment_cmd import holdout_log_path

        main(["init", "--agent-type", "code", "--dest", str(store)])
        log = holdout_log_path(str(store))
        os.makedirs(os.path.dirname(log), exist_ok=True)
        row = json.dumps({"occasion_id": "t1", "lesson": "l", "injected": True, "rate": 0.5})
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(row + "\n" + row + "\n")
        _write_episode(store, "t1", "CONFORM", 1)
        capsys.readouterr()
        assert main(["experiment", "--dest", str(store)]) == 0
        assert "duplicate assignment" in capsys.readouterr().out


class TestSemanticPathRunsTheExperiment:
    """The holdout must apply on BOTH retrieval paths.

    Only the lexical branch honoured `--experiment`; the semantic branch
    forwarded just the query and --top-k to the reference script. So a fleet
    with the `attention` extra installed -- the recommended production setup --
    could run `query --experiment` on every task forever and `commontrace
    experiment` would keep reporting "no holdout assignments recorded yet".
    The causal feature silently did nothing exactly where it was meant to run.
    """

    SEMANTIC_STDOUT = (
        "# Top-10 retrieval (+ importance>=4 override)\n"
        "# Index: 3 lessons, model=all-MiniLM-L6-v2\n"
        "# Query: 'refund delayed'\n"
        "lesson_alpha | cosine=0.812 | importance=4\n"
        "lesson_beta | cosine=0.640 | importance=3\n"
        "lesson_gamma | cosine=0.501 | importance=2\n"
    )

    @pytest.fixture
    def semantic(self, monkeypatch):
        from commontrace.commands import query_cmd
        monkeypatch.setattr(query_cmd, "has_attention_deps", lambda: True)
        monkeypatch.setattr(
            query_cmd, "run_script",
            lambda root, rel, args, hint, capture=False: (
                (0, self.SEMANTIC_STDOUT) if capture else 0
            ),
        )
        return query_cmd

    def test_arms_are_logged_on_the_semantic_path(self, store, semantic, capsys):
        from commontrace.commands.experiment_cmd import holdout_log_path

        main(["init", "--agent-type", "code", "--dest", str(store)])
        capsys.readouterr()
        rc = main(["query", "refund delayed", "--experiment",
                   "--occasion-id", "task-1", "--holdout-rate", "0.5", "--dest", str(store)])
        assert rc == 0

        records = [
            json.loads(line)
            for line in open(holdout_log_path(str(store)), encoding="utf-8")
            if line.strip()
        ]
        assert {r["lesson"] for r in records} == {"lesson_alpha", "lesson_beta", "lesson_gamma"}
        assert all(r["occasion_id"] == "task-1" for r in records)

    def test_withheld_lessons_are_marked_in_the_output(self, store, semantic, capsys):
        main(["init", "--agent-type", "code", "--dest", str(store)])
        capsys.readouterr()
        assert main(["query", "refund delayed", "--experiment", "--occasion-id", "t",
                     "--holdout-rate", "1.0", "--dest", str(store)]) == 0
        out = capsys.readouterr().out
        # rate 1.0 withholds everything
        assert out.count("[WITHHELD - holdout]") == 3
        assert "cosine=" not in out.replace("# ", "")

    def test_header_lines_are_not_mistaken_for_lessons(self, store, semantic, capsys):
        from commontrace.commands.experiment_cmd import holdout_log_path

        main(["init", "--agent-type", "code", "--dest", str(store)])
        capsys.readouterr()
        main(["query", "q", "--experiment", "--occasion-id", "t", "--dest", str(store)])
        slugs = {
            json.loads(line)["lesson"]
            for line in open(holdout_log_path(str(store)), encoding="utf-8") if line.strip()
        }
        assert not any(s.startswith("#") for s in slugs)
        assert len(slugs) == 3

    def test_occasion_id_is_still_required(self, store, semantic, capsys):
        main(["init", "--agent-type", "code", "--dest", str(store)])
        capsys.readouterr()
        assert main(["query", "q", "--experiment", "--dest", str(store)]) == 1
        assert "requires --occasion-id" in capsys.readouterr().err

    def test_without_experiment_the_path_is_unchanged(self, store, semantic, capsys, monkeypatch):
        """No capture, no post-processing: plain passthrough as before."""
        calls = []
        from commontrace.commands import query_cmd
        monkeypatch.setattr(
            query_cmd, "run_script",
            lambda root, rel, args, hint, capture=False: (calls.append(capture), 0)[1],
        )
        main(["init", "--agent-type", "code", "--dest", str(store)])
        assert main(["query", "q", "--dest", str(store)]) == 0
        assert calls == [False]

    def test_an_unsupported_agent_type_filter_is_announced(self, store, semantic, capsys):
        """The semantic script has no agent_type filter. Silently returning
        unfiltered results that look filtered is the worse failure."""
        main(["init", "--agent-type", "code", "--dest", str(store)])
        capsys.readouterr()
        main(["query", "q", "--agent-type", "support", "--dest", str(store)])
        assert "not supported by the semantic retriever" in capsys.readouterr().err
