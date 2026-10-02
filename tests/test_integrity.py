from __future__ import annotations

import datetime
import random

import pytest

from commontrace import experiment, integrity

UTC = datetime.timezone.utc


def a(lesson="lesson_x", occasion="occ", injected=True, succeeded=None,
      rate=0.5, salt="default", at=None, rev="rev-aaa") -> integrity.Assignment:
    return integrity.Assignment(lesson=lesson, occasion_id=occasion, injected=injected,
                                rate=rate, salt=salt, succeeded=succeeded, at=at,
                                revision=rev)


def observations(rows: list[integrity.Assignment]) -> list[experiment.HoldoutObservation]:
    unique, _ = integrity.normalize(rows)
    return [
        experiment.HoldoutObservation(r.lesson, r.occasion_id, r.injected, bool(r.succeeded))
        for r in unique if r.succeeded is not None
    ]


def a_null_fleet_with_unequal_reporting(
    n: int = 600, *, drop_withheld_failures: float = 0.55, seed: int = 7,
) -> list[integrity.Assignment]:
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        injected = i % 2 == 0
        succeeded = rng.random() < 0.50
        reported = not (not succeeded and not injected and rng.random() < drop_withheld_failures)
        rows.append(a(occasion=f"occ-{i}", injected=injected,
                      succeeded=succeeded if reported else None))
    return rows


class TestThePremise:
    def test_attrition_alone_manufactures_a_significant_verdict(self):
        rows = a_null_fleet_with_unequal_reporting()
        effect = experiment.analyze(observations(rows))[0]

        assert effect.significant, "the demonstration requires a significant result"
        assert effect.verdict in (experiment.VERDICT_HELPS, experiment.VERDICT_HURTS)
        assert abs(effect.effect) > 0.05
        assert effect.p_value < 0.01
        assert not (effect.ci_low <= 0.0 <= effect.ci_high)

    def test_and_the_audit_catches_it(self):
        report = integrity.audit(a_null_fleet_with_unequal_reporting())
        assert report.verdict == integrity.VERDICT_COMPROMISED
        assert not report.readable
        assert [f.check for f in report.blocking] == ["differential_attrition"]

    def test_the_finding_names_the_direction_the_estimate_is_pushed(self):
        finding = integrity.check_differential_attrition(a_null_fleet_with_unequal_reporting())
        assert "UNDERSTATES" in finding.detail
        assert finding.numbers["withheld_rate"] < finding.numbers["injected_rate"]

    def test_the_mirror_image_is_reported_as_overstating(self):
        rng = random.Random(3)
        rows = []
        for i in range(600):
            injected = i % 2 == 0
            succeeded = rng.random() < 0.5
            reported = not (not succeeded and injected and rng.random() < 0.55)
            rows.append(a(occasion=f"o{i}", injected=injected,
                          succeeded=succeeded if reported else None))
        finding = integrity.check_differential_attrition(rows)
        assert finding.severity == integrity.SEVERITY_INVALIDATES
        assert "OVERSTATES" in finding.detail


class TestDifferentialAttrition:
    def test_a_clean_run_passes(self):
        rows = [a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=i % 3 == 0)
                for i in range(200)]
        finding = integrity.check_differential_attrition(rows)
        assert finding.severity == integrity.SEVERITY_OK
        assert integrity.audit(rows).verdict == integrity.VERDICT_SOUND

    def test_heavy_but_symmetric_attrition_weakens_rather_than_invalidates(self):
        rng = random.Random(5)
        rows = [a(occasion=f"o{i}", injected=i % 2 == 0,
                  succeeded=(rng.random() < 0.5) if rng.random() > 0.45 else None)
                for i in range(600)]
        finding = integrity.check_differential_attrition(rows)
        assert finding.severity == integrity.SEVERITY_WEAKENS
        assert "does not bias the estimate, it shrinks it" in finding.detail
        report = integrity.audit(rows)
        assert report.verdict == integrity.VERDICT_WEAKENED
        assert report.readable

    def test_one_empty_arm_is_reported_as_not_checkable_not_as_clean(self):
        rows = [a(occasion=f"o{i}", injected=True, succeeded=True) for i in range(50)]
        finding = integrity.check_differential_attrition(rows)
        assert finding.severity == integrity.SEVERITY_OK
        assert "nothing to compare" in finding.detail


class TestArmBalance:
    def test_a_rate_far_from_configured_is_flagged(self):
        rows = [a(occasion=f"o{i}", injected=i % 2 == 0, rate=0.10, succeeded=True)
                for i in range(200)]
        finding = integrity.check_arm_balance(rows)
        assert finding.severity == integrity.SEVERITY_INVALIDATES
        assert "not sampling noise" in finding.detail

    def test_a_matching_rate_passes(self):
        rows = [a(occasion=f"o{i}", injected=i % 10 != 0, rate=0.10, succeeded=True)
                for i in range(200)]
        assert integrity.check_arm_balance(rows).severity == integrity.SEVERITY_OK

    def test_a_correct_randomizer_is_essentially_never_flagged(self):
        rng = random.Random(99)
        trials, flagged = 2000, 0
        for _ in range(trials):
            rows = [a(occasion=f"o{i}", injected=rng.random() >= 0.5, rate=0.5, succeeded=True)
                    for i in range(60)]
            flagged += integrity.check_arm_balance(rows).severity != integrity.SEVERITY_OK
        assert flagged / trials < 0.01, f"{flagged}/{trials} sound runs flagged"

    @pytest.mark.parametrize("configured,injected_when,n", [
        (0.10, lambda i: i % 2 == 0, 60),
        (0.50, lambda i: True, 40),
        (0.20, lambda i: i % 5 >= 2, 100),
        (0.10, lambda i: i % 5 != 0, 200),
    ])
    def test_every_realistic_breakage_is_still_caught(self, configured, injected_when, n):
        rows = [a(occasion=f"o{i}", injected=injected_when(i), rate=configured, succeeded=True)
                for i in range(n)]
        assert integrity.check_arm_balance(rows).severity == integrity.SEVERITY_INVALIDATES

    def test_its_alpha_is_far_stricter_than_the_attrition_one(self):
        assert integrity.ARM_BALANCE_ALPHA < integrity.VALIDITY_ALPHA / 50

    def test_a_small_sample_is_not_flagged_for_ordinary_noise(self):
        rows = [a(occasion=f"o{i}", injected=i % 2 == 0, rate=0.10, succeeded=True)
                for i in range(20)]
        finding = integrity.check_arm_balance(rows)
        assert finding.severity == integrity.SEVERITY_OK
        assert "too few to test yet" in finding.headline


class TestReRandomization:
    def test_a_salt_change_mid_run_invalidates(self):
        rows = ([a(occasion=f"o{i}", salt="one", succeeded=True) for i in range(20)]
                + [a(occasion=f"p{i}", salt="two", succeeded=True) for i in range(20)])
        finding = integrity.check_assignment_drift(rows)
        assert finding.severity == integrity.SEVERITY_INVALIDATES
        assert "two different experiments pooled" in finding.detail

    def test_a_rate_change_mid_run_invalidates(self):
        rows = ([a(occasion=f"o{i}", rate=0.1, succeeded=True) for i in range(20)]
                + [a(occasion=f"p{i}", rate=0.5, succeeded=True) for i in range(20)])
        assert integrity.check_assignment_drift(rows).severity == integrity.SEVERITY_INVALIDATES

    def test_one_randomization_passes(self):
        rows = [a(occasion=f"o{i}", succeeded=True) for i in range(20)]
        assert integrity.check_assignment_drift(rows).severity == integrity.SEVERITY_OK


class TestConflictingArms:
    def test_the_same_pair_in_both_arms_is_caught(self):
        rows = [a(occasion="o1", injected=True, succeeded=True),
                a(occasion="o1", injected=False, succeeded=False)]
        finding = integrity.check_inconsistent_arms(rows)
        assert finding.severity == integrity.SEVERITY_INVALIDATES
        assert finding.numbers["conflicted"] == 1

    def test_it_survives_the_retry_collapse(self):
        rows = [a(occasion="o1", injected=True, succeeded=True),
                a(occasion="o1", injected=False, succeeded=False)]
        report = integrity.audit(rows)
        assert report.verdict == integrity.VERDICT_COMPROMISED
        assert any(f.check == "consistent_arms" for f in report.blocking)

    def test_an_identical_retry_is_not_a_conflict(self):
        rows = [a(occasion="o1", injected=True, succeeded=True)] * 3
        assert integrity.check_inconsistent_arms(rows).severity == integrity.SEVERITY_OK


class TestRetriesAreCollapsedOnce:
    def test_normalize_keeps_one_row_per_pair(self):
        rows = [a(occasion="o1"), a(occasion="o1"), a(occasion="o2")]
        unique, duplicates = integrity.normalize(rows)
        assert len(unique) == 2 and duplicates == 1

    def test_the_audit_and_the_estimate_count_the_same_assignments(self):
        rows = [a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=True) for i in range(50)]
        rows += rows[:10]
        report = integrity.audit(rows)
        assert report.n_assignments == 50
        assert report.n_duplicates == 10
        assert len(observations(rows)) == report.n_resolved


class TestOutcomeVariation:
    def test_an_outcome_nothing_can_fail_is_flagged(self):
        rows = [a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=True) for i in range(60)]
        finding = integrity.check_outcome_variation(rows)
        assert finding.severity == integrity.SEVERITY_INVALIDATES
        assert "nothing for a lesson to move" in finding.detail

    def test_a_small_sample_is_not_judged(self):
        rows = [a(occasion=f"o{i}", succeeded=True) for i in range(10)]
        assert integrity.check_outcome_variation(rows).severity == integrity.SEVERITY_OK

    def test_variation_passes(self):
        rows = [a(occasion=f"o{i}", succeeded=i % 4 == 0) for i in range(60)]
        assert integrity.check_outcome_variation(rows).severity == integrity.SEVERITY_OK


class TestPowerProjection:
    def test_it_names_the_control_arm_and_what_the_rate_costs(self):
        base = datetime.datetime(2026, 1, 1, tzinfo=UTC)
        rows = [a(occasion=f"o{i}", injected=i % 10 != 0, rate=0.10, succeeded=True,
                  at=base + datetime.timedelta(hours=i))
                for i in range(50)]
        p = integrity.project(rows)[0]
        assert p.binding_arm == "withheld"
        assert p.still_needed == experiment.DEFAULT_MIN_ARM - 5
        assert "control arm binds" in p.advice
        assert "100 occasions" in p.advice

    def test_it_projects_a_date_from_the_accrual_rate(self):
        base = datetime.datetime(2026, 1, 1, tzinfo=UTC)
        rows = [a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=True,
                  at=base + datetime.timedelta(days=i))
                for i in range(10)]
        p = integrity.project(rows)[0]
        assert p.per_day and p.eta is not None
        assert p.eta > base.date()

    def test_an_undated_log_degrades_to_no_projection_rather_than_failing(self):
        rows = [a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=True) for i in range(10)]
        p = integrity.project(rows)[0]
        assert p.per_day is None and p.eta is None and p.still_needed > 0

    def test_a_powered_lesson_says_so(self):
        rows = [a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=i % 3 == 0)
                for i in range(60)]
        p = integrity.project(rows)[0]
        assert p.still_needed == 0 and "Powered" in p.advice

    def test_a_glacial_accrual_rate_does_not_produce_a_date(self):
        base = datetime.datetime(2026, 1, 1, tzinfo=UTC)
        rows = [a(occasion="o0", injected=False, succeeded=True, at=base),
                a(occasion="o1", injected=False, succeeded=True,
                  at=base + datetime.timedelta(days=4000))]
        p = integrity.project(rows)[0]
        assert p.eta is None


class TestTheReport:
    def test_it_states_what_it_cannot_check(self):
        text = integrity.render(integrity.audit([a(occasion="o", succeeded=True)]))
        assert "NOT checkable here" in text
        assert "withhold" in text

    def test_passing_checks_are_shown_not_filtered_out(self):
        text = integrity.render(integrity.audit(
            [a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=i % 3 == 0) for i in range(60)]))
        assert text.count("[OK]") >= 4

    def test_a_compromised_report_says_more_data_will_not_help(self):
        text = integrity.render(integrity.audit(a_null_fleet_with_unequal_reporting()))
        assert "Do not quote the effect sizes below" in text
        assert "more data will not fix it" in text

    def test_an_empty_experiment_is_sound_rather_than_crashing(self):
        report = integrity.audit([])
        assert report.verdict == integrity.VERDICT_SOUND
        assert integrity.render(report)


class TestTheValidityAlphaIsDeliberatelyLoose:
    def test_it_is_looser_than_the_effect_alpha(self):
        assert integrity.VALIDITY_ALPHA > 0.05

    @pytest.mark.parametrize("gap", [0.10, 0.15, 0.20])
    def test_a_moderate_reporting_gap_is_still_caught_at_scale(self, gap):
        rng = random.Random(int(gap * 100))
        rows = []
        for i in range(1000):
            injected = i % 2 == 0
            reported = injected or rng.random() > gap
            rows.append(a(occasion=f"o{i}", injected=injected,
                          succeeded=(rng.random() < 0.5) if reported else None))
        assert integrity.check_differential_attrition(rows).severity == \
            integrity.SEVERITY_INVALIDATES


class TestTheExperimentCommand:
    @staticmethod
    def _store(tmp_path, rows: list[integrity.Assignment], outcomes: dict[str, bool]):
        import json
        import os
        import subprocess
        import sys

        from commontrace import frontmatter, paths, templates

        root = str(tmp_path / "fleet")
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        def cli(*argv):
            return subprocess.run([sys.executable, "-m", "commontrace.cli", *argv],
                                  capture_output=True, text=True, cwd=repo, check=False)

        assert cli("init", "--dest", root, "--agent-type", "support").returncode == 0

        tdir = paths.traces_dir(root)
        os.makedirs(tdir, exist_ok=True)
        for occasion, succeeded in outcomes.items():
            fm = templates.trace_frontmatter(
                occasion, f"Case {occasion}", "support", [], "",
                {"resolved": bool(succeeded)},
            )
            frontmatter.write(
                os.path.join(tdir, f"2026-01-01_case_{occasion}.md"), fm,
                templates.trace_body(
                    "A customer reported a problem with their account.",
                    "The problem was investigated and answered."),
            )

        log = os.path.join(paths.memory_dir(root), "holdout_log.jsonl")
        with open(log, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({
                    "occasion_id": r.occasion_id, "lesson": r.lesson,
                    "injected": r.injected, "rate": r.rate, "salt": r.salt,
                    **({"at": r.at.isoformat()} if r.at else {}),
                    **({"revision": r.revision} if r.revision else {}),
                }) + "\n")
        return root, cli

    def test_a_compromised_run_says_so_before_it_shows_a_number(self, tmp_path):
        rows = a_null_fleet_with_unequal_reporting(n=200)
        outcomes = {r.occasion_id: bool(r.succeeded) for r in rows if r.succeeded is not None}
        root, cli = self._store(tmp_path, rows, outcomes)

        result = cli("experiment", "--dest", root)
        assert result.returncode == 0, result.stderr
        out = result.stdout
        assert "Compromised" in out
        assert out.index("Can this be trusted?") < out.index("Causal Effect Report")
        assert "not equally observed" in out

    def test_strict_fails_a_compromised_run(self, tmp_path):
        rows = a_null_fleet_with_unequal_reporting(n=200)
        outcomes = {r.occasion_id: bool(r.succeeded) for r in rows if r.succeeded is not None}
        root, cli = self._store(tmp_path, rows, outcomes)

        result = cli("experiment", "--dest", root, "--strict")
        assert result.returncode == 1
        assert "COMPROMISED" in result.stderr

    def test_a_sound_run_reads_clean_and_passes_strict(self, tmp_path):
        base = datetime.datetime(2026, 1, 1, tzinfo=UTC)
        rows = [a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=(i % 3 == 0),
                  at=base + datetime.timedelta(hours=i)) for i in range(120)]
        outcomes = {r.occasion_id: bool(r.succeeded) for r in rows}
        root, cli = self._store(tmp_path, rows, outcomes)

        result = cli("experiment", "--dest", root, "--strict")
        assert result.returncode == 0, result.stderr
        assert "**Sound.**" in result.stdout

    def test_json_carries_the_integrity_report(self, tmp_path):
        import json as _json

        rows = a_null_fleet_with_unequal_reporting(n=200)
        outcomes = {r.occasion_id: bool(r.succeeded) for r in rows if r.succeeded is not None}
        root, cli = self._store(tmp_path, rows, outcomes)

        result = cli("experiment", "--dest", root, "--json")
        assert result.returncode == 0, result.stderr
        payload = _json.loads(result.stdout)
        assert payload["integrity"]["verdict"] == integrity.VERDICT_COMPROMISED
        assert any(f["severity"] == integrity.SEVERITY_INVALIDATES
                   for f in payload["integrity"]["findings"])

    def test_an_old_undated_log_still_reports(self, tmp_path):
        rows = [a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=(i % 3 == 0), at=None)
                for i in range(60)]
        outcomes = {r.occasion_id: bool(r.succeeded) for r in rows}
        root, cli = self._store(tmp_path, rows, outcomes)

        result = cli("experiment", "--dest", root)
        assert result.returncode == 0, result.stderr
        assert "Can this be trusted?" in result.stdout


class TestTreatmentStability:
    def test_a_lesson_edited_mid_run_invalidates(self):
        rows = ([a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=i % 3 == 0, rev="aaa")
                 for i in range(20)]
                + [a(occasion=f"p{i}", injected=i % 2 == 0, succeeded=i % 3 == 0, rev="bbb")
                   for i in range(20)])
        finding = integrity.check_treatment_stability(rows)
        assert finding.severity == integrity.SEVERITY_INVALIDATES
        assert finding.numbers["changed"] == {"lesson_x": ["aaa", "bbb"]}
        assert "no longer exists" in finding.detail

    def test_it_blocks_the_whole_report(self):
        rows = ([a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=i % 3 == 0, rev="aaa")
                 for i in range(20)]
                + [a(occasion=f"p{i}", injected=i % 2 == 0, succeeded=i % 3 == 0, rev="bbb")
                   for i in range(20)])
        report = integrity.audit(rows)
        assert report.verdict == integrity.VERDICT_COMPROMISED
        assert [f.check for f in report.blocking] == ["treatment_stability"]

    def test_one_lesson_moving_does_not_implicate_another(self):
        rows = ([a(lesson="stable", occasion=f"o{i}", injected=i % 2 == 0,
                   succeeded=i % 3 == 0, rev="aaa") for i in range(20)]
                + [a(lesson="moved", occasion=f"o{i}", injected=i % 2 == 0,
                     succeeded=i % 3 == 0, rev="bbb" if i > 10 else "ccc")
                   for i in range(20)])
        finding = integrity.check_treatment_stability(rows)
        assert set(finding.numbers["changed"]) == {"moved"}

    def test_a_stable_lesson_passes(self):
        rows = [a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=i % 3 == 0)
                for i in range(40)]
        assert integrity.check_treatment_stability(rows).severity == integrity.SEVERITY_OK
        assert integrity.audit(rows).verdict == integrity.VERDICT_SOUND

    def test_an_unversioned_log_is_unchecked_not_clean(self):
        rows = [a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=i % 3 == 0, rev=None)
                for i in range(40)]
        finding = integrity.check_treatment_stability(rows)
        assert finding.severity == integrity.SEVERITY_WEAKENS
        assert "cannot be checked" in finding.headline
        assert integrity.audit(rows).verdict == integrity.VERDICT_WEAKENED

    def test_a_partly_versioned_log_checks_what_it_can(self):
        rows = ([a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=i % 3 == 0, rev=None)
                 for i in range(10)]
                + [a(occasion=f"p{i}", injected=i % 2 == 0, succeeded=i % 3 == 0, rev="aaa")
                   for i in range(30)])
        finding = integrity.check_treatment_stability(rows)
        assert finding.severity == integrity.SEVERITY_OK
        assert finding.numbers["assignments_without_a_revision"] == 10
        assert "not checked" in finding.headline

    def test_an_empty_experiment_has_nothing_to_check(self):
        finding = integrity.check_treatment_stability([])
        assert finding.severity == integrity.SEVERITY_OK
        assert integrity.audit([]).verdict == integrity.VERDICT_SOUND

    def test_the_report_says_the_text_was_checked(self):
        text = integrity.render(integrity.audit(
            [a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=i % 3 == 0) for i in range(40)]))
        assert "whether the lesson text held still" in text


class TestThePlanCommand:
    @staticmethod
    def _cli(*argv):
        import os
        import subprocess
        import sys

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run([sys.executable, "-m", "commontrace.cli", *argv],
                              capture_output=True, text=True, cwd=repo, check=False)

    def test_it_runs_on_an_empty_store_and_assumes_the_worst(self, tmp_path):
        root = str(tmp_path / "fleet")
        assert self._cli("init", "--dest", root, "--agent-type", "code").returncode == 0
        result = self._cli("experiment", "--plan", "--dest", root)
        assert result.returncode == 0, result.stderr
        assert "50%" in result.stdout
        assert "most pessimistic" in result.stdout

    def test_an_infeasible_design_exits_non_zero(self, tmp_path):
        root = str(tmp_path / "fleet")
        assert self._cli("init", "--dest", root, "--agent-type", "code").returncode == 0
        result = self._cli("experiment", "--plan", "--occasions", "50",
                           "--detect", "0.02", "--dest", root)
        assert result.returncode == 1
        assert "cannot answer this at any holdout rate" in result.stdout

    def test_it_refuses_an_impossible_target(self, tmp_path):
        root = str(tmp_path / "fleet")
        assert self._cli("init", "--dest", root, "--agent-type", "code").returncode == 0
        result = self._cli("experiment", "--plan", "--detect", "1.5", "--dest", root)
        assert result.returncode == 1
        assert "strictly between 0 and 1" in result.stderr

    def test_json_is_machine_readable(self, tmp_path):
        import json as _json

        root = str(tmp_path / "fleet")
        assert self._cli("init", "--dest", root, "--agent-type", "code").returncode == 0
        result = self._cli("experiment", "--plan", "--occasions", "4000",
                           "--detect", "0.15", "--json", "--dest", root)
        payload = _json.loads(result.stdout)
        assert payload["n_per_arm"] > 0 and payload["verdict"] in ("ok", "raise_rate")


class TestTheStoreOwnsItsExperimentSettings:
    @staticmethod
    def _store(tmp_path):
        import os

        from commontrace import paths

        root = str(tmp_path / "fleet")
        os.makedirs(paths.memory_dir(root), exist_ok=True)
        return root

    def test_an_unconfigured_store_reports_the_defaults(self, tmp_path):
        from commontrace import experiment, holdout_io

        config = holdout_io.load_config(self._store(tmp_path))
        assert config.rate == experiment.DEFAULT_HOLDOUT_RATE
        assert config.salt == holdout_io.DEFAULT_SALT
        assert config.running

    def test_configuring_rotates_the_salt(self, tmp_path):
        from commontrace import holdout_io

        root = self._store(tmp_path)
        first = holdout_io.configure(root, rate=0.2)
        second = holdout_io.configure(root, rate=0.5)
        assert first.salt != second.salt
        assert holdout_io.load_config(root).salt == second.salt

    def test_the_salt_records_when_the_run_began(self, tmp_path):
        from commontrace import holdout_io

        config = holdout_io.configure(self._store(tmp_path), rate=0.5)
        assert config.salt.startswith("20") and "0.5" in config.salt

    def test_rate_zero_stops_the_experiment(self, tmp_path):
        from commontrace import holdout_io

        root = self._store(tmp_path)
        holdout_io.configure(root, rate=0.5)
        assert not holdout_io.configure(root, rate=0.0).running

    @pytest.mark.parametrize("bad", [-0.1, 1.0, 1.5])
    def test_an_impossible_rate_is_refused(self, tmp_path, bad):
        from commontrace import holdout_io

        with pytest.raises(ValueError):
            holdout_io.configure(self._store(tmp_path), rate=bad)

    def test_a_corrupt_config_falls_back_rather_than_failing_retrieval(self, tmp_path):
        from commontrace import holdout_io

        root = self._store(tmp_path)
        with open(holdout_io.config_path(root), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        assert holdout_io.load_config(root).rate == \
            integrity.experiment.DEFAULT_HOLDOUT_RATE

    def test_both_retrievers_read_the_same_configured_rate(self, tmp_path):
        import argparse

        from commontrace import holdout_io
        from commontrace.commands import query_cmd

        root = self._store(tmp_path)
        config = holdout_io.configure(root, rate=0.42)

        args = argparse.Namespace(holdout_rate=None, experiment_salt=None)
        assert query_cmd._effective_holdout(args, root) == (0.42, config.salt)

        override = argparse.Namespace(holdout_rate=0.9, experiment_salt="other")
        assert query_cmd._effective_holdout(override, root) == (0.9, "other")


class TestAnalysisIsScopedToOneRandomization:
    @staticmethod
    def _cli(*argv):
        import os
        import subprocess
        import sys

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run([sys.executable, "-m", "commontrace.cli", *argv],
                              capture_output=True, text=True, cwd=repo, check=False)

    def _seeded(self, tmp_path, salt):
        rows = [a(occasion=f"{salt}-{i}", injected=i % 2 == 0, succeeded=i % 3 == 0,
                  salt=salt) for i in range(40)]
        outcomes = {r.occasion_id: bool(r.succeeded) for r in rows}
        return TestTheExperimentCommand._store(tmp_path, rows, outcomes)

    def test_an_earlier_run_is_not_pooled_into_the_current_one(self, tmp_path):
        from commontrace import holdout_io

        root, cli = self._seeded(tmp_path, "old-salt")
        holdout_io.configure(root, rate=0.5)

        result = cli("experiment", "--dest", root)
        assert result.returncode == 0
        assert "none under the current randomization" in result.stderr
        assert "old-salt" in result.stderr

    def test_an_earlier_run_is_still_readable_by_name(self, tmp_path):
        from commontrace import holdout_io

        root, cli = self._seeded(tmp_path, "old-salt")
        holdout_io.configure(root, rate=0.5)

        result = cli("experiment", "--salt", "old-salt", "--dest", root)
        assert result.returncode == 0, result.stderr
        assert "Causal Effect Report" in result.stdout

    def test_an_unconfigured_store_is_unaffected(self, tmp_path):
        root, cli = self._seeded(tmp_path, "default")
        result = cli("experiment", "--dest", root)
        assert result.returncode == 0, result.stderr
        assert "Causal Effect Report" in result.stdout

    def test_the_reported_holdout_rate_is_the_current_salts_not_a_blend(self, tmp_path):
        import json

        from commontrace import holdout_io

        old_rows = [a(occasion=f"old-{i}", injected=i % 2 == 0, succeeded=i % 3 == 0,
                      rate=0.1, salt="old-salt") for i in range(40)]
        outcomes = {r.occasion_id: bool(r.succeeded) for r in old_rows}
        root, cli = TestTheExperimentCommand._store(tmp_path, old_rows, outcomes)

        config = holdout_io.configure(root, rate=0.5)
        new_rows = [a(occasion=f"new-{i}", injected=i % 2 == 0, succeeded=i % 3 == 0,
                      rate=0.5, salt=config.salt) for i in range(40)]

        import os

        from commontrace import frontmatter, paths, templates

        tdir = paths.traces_dir(root)
        for r in new_rows:
            fm = templates.trace_frontmatter(
                r.occasion_id, f"Case {r.occasion_id}", "support", [], "",
                {"resolved": bool(r.succeeded)},
            )
            frontmatter.write(
                os.path.join(tdir, f"2026-01-02_case_{r.occasion_id}.md"), fm,
                templates.trace_body("A customer reported a problem.", "Investigated and answered."),
            )
        log = os.path.join(paths.memory_dir(root), "holdout_log.jsonl")
        with open(log, "a", encoding="utf-8") as fh:
            for r in new_rows:
                fh.write(json.dumps({
                    "occasion_id": r.occasion_id, "lesson": r.lesson,
                    "injected": r.injected, "rate": r.rate, "salt": r.salt,
                }) + "\n")

        result = cli("experiment", "--dest", root)
        assert result.returncode == 0, result.stderr
        assert "Holdout rate: **50%**" in result.stdout, result.stdout
        assert "Holdout rate: **30%**" not in result.stdout


class TestAnOldLogStaysReadable:
    def test_a_line_with_no_salt_belongs_to_the_default_randomization(self, tmp_path):
        import json
        import os

        from commontrace import holdout_io, paths

        root = str(tmp_path / "fleet")
        os.makedirs(paths.memory_dir(root), exist_ok=True)
        with open(holdout_io.holdout_log_path(root), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"occasion_id": "t1", "lesson": "l", "injected": True,
                                 "rate": 0.5}) + "\n")
        records, corrupt = holdout_io.read_log(root)
        assert corrupt == 0
        assert [r.salt for r in records] == [holdout_io.DEFAULT_SALT]

    def test_such_a_log_still_produces_a_report(self, tmp_path):
        import json
        import os

        from commontrace import holdout_io, paths

        rows = [a(occasion=f"o{i}", injected=i % 2 == 0, succeeded=i % 3 == 0)
                for i in range(40)]
        outcomes = {r.occasion_id: bool(r.succeeded) for r in rows}
        root, cli = TestTheExperimentCommand._store(tmp_path, rows, outcomes)

        with open(holdout_io.holdout_log_path(root), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({"occasion_id": r.occasion_id, "lesson": r.lesson,
                                     "injected": r.injected, "rate": r.rate}) + "\n")
        assert os.path.isfile(holdout_io.holdout_log_path(root))
        assert paths.memory_dir(root)

        result = cli("experiment", "--dest", root)
        assert result.returncode == 0, result.stderr
        assert "Causal Effect Report" in result.stdout
        assert "none under the current randomization" not in result.stderr


class TestReadLogSurvivesANonFiniteRank:
    def test_an_infinite_rank_is_dropped_not_fatal(self, tmp_path):
        import json
        import os

        from commontrace import holdout_io, paths

        root = str(tmp_path / "fleet")
        os.makedirs(paths.memory_dir(root), exist_ok=True)
        with open(holdout_io.holdout_log_path(root), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "occasion_id": "t1", "lesson": "l", "injected": True, "rank": float("inf"),
            }) + "\n")
            fh.write(json.dumps({
                "occasion_id": "t2", "lesson": "l", "injected": False, "rank": 3,
            }) + "\n")

        records, corrupt = holdout_io.read_log(root)
        assert corrupt == 0
        assert len(records) == 2
        assert records[0].rank is None
        assert records[1].rank == 3
