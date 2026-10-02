from __future__ import annotations

import datetime as dt

from commontrace import integrity, survival

NOW = dt.datetime(2026, 9, 11, 12, 0, tzinfo=dt.timezone.utc)


def _obs(duration: float, event: bool) -> survival.Observation:
    return survival.Observation(duration=duration, event=event)


class TestKaplanMeier:
    def test_every_occasion_reported_walks_survival_to_zero(self):
        curve = survival.kaplan_meier([_obs(t, True) for t in (1, 2, 3, 4)])
        assert [round(s.survival, 3) for s in curve] == [0.75, 0.5, 0.25, 0.0]

    def test_censored_occasions_leave_the_risk_set_without_counting_as_events(self):
        curve = survival.kaplan_meier(
            [_obs(1, True), _obs(2, True), _obs(9, False), _obs(9, False)]
        )
        assert round(curve[-1].survival, 3) == 0.5
        assert survival.time_to_reported_fraction(curve, 0.9) is None

    def test_ties_at_one_instant_are_one_rung(self):
        curve = survival.kaplan_meier([_obs(5, True), _obs(5, True), _obs(5, True)])
        assert len(curve) == 1
        assert curve[0].events == 3

    def test_an_empty_log_has_no_curve(self):
        assert survival.kaplan_meier([]) == []

    def test_reported_fraction_reads_the_step_below_t(self):
        curve = survival.kaplan_meier([_obs(t, True) for t in (1, 2, 3, 4)])
        assert survival.reported_fraction_at(curve, 0.5) == 0.0
        assert survival.reported_fraction_at(curve, 2.0) == 0.5
        assert survival.reported_fraction_at(curve, 99.0) == 1.0


class TestLogRank:
    def test_identical_schedules_are_not_a_difference(self):
        arm = [_obs(float(i), True) for i in range(1, 21)]
        z, p = survival.log_rank_test(arm, list(arm))
        assert z == 0.0 and p == 1.0

    def test_a_clear_speed_difference_is_detected(self):
        fast = [_obs(float(i), True) for i in range(1, 21)]
        slow = [_obs(float(i) + 100, True) for i in range(1, 21)]
        z, p = survival.log_rank_test(fast, slow)
        assert p < 1e-6
        assert z > 0, "positive z must mean the FIRST arm reported sooner"

    def test_an_empty_arm_is_unanswerable_not_an_error(self):
        assert survival.log_rank_test([], [_obs(1, True)]) == (0.0, 1.0)

    def test_no_events_at_all_is_unanswerable(self):
        pending = [_obs(float(i), False) for i in range(10)]
        assert survival.log_rank_test(pending, list(pending)) == (0.0, 1.0)


def _fleet(treated_takes_min, control_takes_min, n=120, span_min=180, lost_control=0):
    rows = []
    for i in range(n):
        age = span_min * i / n
        at = NOW - dt.timedelta(minutes=age)
        for injected, takes, tag in (
            (True, treated_takes_min, "i"),
            (False, control_takes_min, "w"),
        ):
            lost = (not injected) and i < lost_control
            reported = (age >= takes) and not lost
            went_well = (i % 10 < 8) if injected else (i % 10 < 5)
            rows.append(
                integrity.Assignment(
                    lesson="L", occasion_id=f"{tag}{i}", injected=injected,
                    rate=0.5,
                    succeeded=went_well if reported else None, at=at,
                    resolved_at=at + dt.timedelta(minutes=takes) if reported else None,
                )
            )
    return rows


class TestTheRunThatWasPunishedForWorking:
    def test_a_fast_treated_arm_is_no_longer_called_attrition(self):
        rows = _fleet(treated_takes_min=5, control_takes_min=90)
        finding = integrity.check_differential_attrition(rows, now=NOW)
        assert finding.severity == integrity.SEVERITY_OK, finding.headline
        assert finding.numbers["pending_too_young"] > 0, (
            "the young control occasions must be set aside, not counted as missing"
        )

    def test_the_same_data_read_without_timestamps_still_shows_the_old_verdict(self):
        rows = _fleet(treated_takes_min=5, control_takes_min=90)
        untimed = [
            integrity.Assignment(
                lesson=r.lesson, occasion_id=r.occasion_id,
                injected=r.injected, succeeded=r.succeeded,
            )
            for r in rows
        ]
        assert (
            integrity.check_differential_attrition(untimed).severity
            == integrity.SEVERITY_INVALIDATES
        )

    def test_genuine_loss_is_still_caught(self):
        rows = _fleet(
            treated_takes_min=5, control_takes_min=6, span_min=600, lost_control=40
        )
        finding = integrity.check_differential_attrition(rows, now=NOW)
        assert finding.severity == integrity.SEVERITY_INVALIDATES, finding.headline

    def test_a_healthy_run_stays_ok(self):
        rows = _fleet(treated_takes_min=5, control_takes_min=6, span_min=600)
        assert (
            integrity.check_differential_attrition(rows, now=NOW).severity
            == integrity.SEVERITY_OK
        )


class TestTheHorizonTakesTheSlowerArm:
    def test_a_slow_control_arm_is_judged_on_its_own_clock(self):
        rows = _fleet(treated_takes_min=5, control_takes_min=90)
        finding = integrity.check_differential_attrition(rows, now=NOW)
        horizon = finding.numbers["maturity_horizon_seconds"]
        assert horizon is not None and horizon >= 90 * 60 * 0.9, (
            f"horizon {horizon}s came from the fast arm, not the slow one"
        )


class TestCensoringHazard:
    def test_a_matched_schedule_is_ok(self):
        rows = _fleet(treated_takes_min=5, control_takes_min=5, span_min=600)
        assert (
            integrity.check_censoring_hazard(rows, now=NOW).severity
            == integrity.SEVERITY_OK
        )

    def test_a_speed_gap_with_work_still_pending_says_wait_not_biased(self):
        rows = _fleet(treated_takes_min=5, control_takes_min=90)
        finding = integrity.check_censoring_hazard(rows, now=NOW)
        assert finding.severity == integrity.SEVERITY_WEAKENS
        assert "NOT about bias" in finding.detail
        assert finding.numbers["pending"] > 0

    def test_it_reports_which_arm_is_ahead(self):
        rows = _fleet(treated_takes_min=5, control_takes_min=90)
        finding = integrity.check_censoring_hazard(rows, now=NOW)
        assert "injected arm is reporting faster" in finding.headline

    def test_an_untimed_log_is_not_checkable_rather_than_wrong(self):
        rows = [
            integrity.Assignment(lesson="L", occasion_id=f"o{i}", injected=i % 2 == 0)
            for i in range(10)
        ]
        finding = integrity.check_censoring_hazard(rows)
        assert finding.severity == integrity.SEVERITY_OK
        assert "not checkable" in finding.headline


class TestTheAuditCarriesIt:
    def test_the_new_check_appears_in_the_report(self):
        rows = _fleet(treated_takes_min=5, control_takes_min=90)
        report = integrity.audit(rows, now=NOW)
        assert any(f.check == "censoring_hazard" for f in report.findings)

    def test_a_working_lesson_no_longer_compromises_its_own_run(self):
        rows = _fleet(treated_takes_min=5, control_takes_min=90)
        report = integrity.audit(rows, now=NOW)
        assert report.verdict != integrity.VERDICT_COMPROMISED
        assert report.readable
