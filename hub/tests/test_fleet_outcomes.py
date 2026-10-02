from __future__ import annotations

import pytest
import pytest_asyncio

from hub import crud, manage, outcomes
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("fleet-a", "fleet-b"):
            o = Organization(name=name)
            session.add(o)
            await session.flush()
            made[name] = o.id
        return made


async def _traces(session_factory, org_id, outcome_list, agent_type="code", quarantined=False):
    async with session_scope(session_factory) as session:
        for i, outcome in enumerate(outcome_list):
            session.add(
                Trace(
                    org_id=org_id,
                    title=f"t{i}",
                    context_text="c",
                    solution_text="s",
                    tags=[],
                    agent_type=agent_type,
                    outcome=outcome,
                    quarantined=quarantined,
                )
            )
        await session.flush()


def _arm(n, *, resolved_true, baseline, **extra):
    return [
        {"resolved": i < resolved_true, "baseline": baseline, **extra} for i in range(n)
    ]


async def _report(session_factory, org_id, **kwargs):
    async with session_scope(session_factory) as session:
        return await crud.fleet_outcomes(session, org_id, **kwargs)


def _row(report, metric):
    return next(r for r in report["metrics"] if r["metric"] == metric)


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestArmsAndRates:
    def test_baseline_splits_on_a_real_bool_only(self):
        base, current = outcomes.split_arms(
            [{"baseline": True}, {"baseline": False}, {}, {"baseline": "true"}]
        )
        assert len(base) == 1
        assert len(current) == 3

    def test_null_fields_leave_the_denominator_rather_than_counting_as_false(self):
        successes, n = outcomes.proportion(
            [{"resolved": True}, {"resolved": None}, {}, {"resolved": False}], "resolved"
        )
        assert (successes, n) == (1, 2)

    def test_a_bool_is_not_averaged_as_a_number(self):
        value, n = outcomes.mean([{"tokens_used": 100}, {"tokens_used": True}], "tokens_used")
        assert (value, n) == (100.0, 1)

    def test_no_baseline_gives_every_metric_insufficient_data(self):
        report = outcomes.compare([], _arm(100, resolved_true=80, baseline=False))
        assert all(r["verdict"] == outcomes.VERDICT_INSUFFICIENT for r in report["metrics"])
        assert "nothing to compare against" in report["headline"]

    def test_the_caveat_is_on_every_report(self):
        report = outcomes.compare([], [])
        assert "NOT A CAUSAL EFFECT" in report["caveat"]


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestHonesty:
    def test_a_real_improvement_is_detected(self):
        report = outcomes.compare(
            _arm(200, resolved_true=100, baseline=True),
            _arm(200, resolved_true=160, baseline=False),
        )
        row = _row(report, "resolution_rate")
        assert row["verdict"] == outcomes.VERDICT_IMPROVED
        assert row["delta"] == pytest.approx(0.30)
        assert row["ci_95"][0] > 0
        assert "improved" in report["headline"]

    def test_a_fleet_that_got_worse_is_reported_as_worse(self):
        report = outcomes.compare(
            _arm(200, resolved_true=160, baseline=True),
            _arm(200, resolved_true=100, baseline=False),
        )
        row = _row(report, "resolution_rate")
        assert row["verdict"] == outcomes.VERDICT_WORSENED
        assert row["delta"] < 0
        assert "WRONG direction" in report["headline"]

    def test_direction_is_per_metric_not_per_sign(self):
        base = [{"repeated_error": i < 160, "baseline": True} for i in range(200)]
        current = [{"repeated_error": i < 100, "baseline": False} for i in range(200)]
        row = _row(outcomes.compare(base, current), "repeated_error_rate")
        assert row["delta"] < 0
        assert row["verdict"] == outcomes.VERDICT_IMPROVED

    def test_a_tiny_sample_is_insufficient_not_null(self):
        report = outcomes.compare(
            _arm(8, resolved_true=4, baseline=True), _arm(8, resolved_true=6, baseline=False)
        )
        row = _row(report, "resolution_rate")
        assert row["verdict"] == outcomes.VERDICT_INSUFFICIENT
        assert "not evidence of no effect" in row["note"]

    def test_an_inconclusive_result_reports_what_it_could_have_detected(self):
        report = outcomes.compare(
            _arm(40, resolved_true=20, baseline=True), _arm(40, resolved_true=22, baseline=False)
        )
        row = _row(report, "resolution_rate")
        assert row["verdict"] == outcomes.VERDICT_INCONCLUSIVE
        assert row["min_detectable_effect"] is not None
        assert "smallest effect" in row["note"]

    def test_the_multiple_comparisons_correction_is_actually_applied(self):
        base = [
            {"resolved": i < 100, "escalated": i < 100, "repeated_error": i < 100,
             "frustration_signal": i < 100, "baseline": True}
            for i in range(200)
        ]
        current = [
            {"resolved": i < 122, "escalated": i < 100, "repeated_error": i < 100,
             "frustration_signal": i < 100, "baseline": False}
            for i in range(200)
        ]
        row = _row(outcomes.compare(base, current), "resolution_rate")
        uncorrected = row["p_value"] < 0.05
        assert uncorrected, "fixture no longer produces a marginal p-value; retune it"
        assert not row["significant"], "Benjamini-Hochberg is not being applied"
        assert row["verdict"] == outcomes.VERDICT_INCONCLUSIVE

    def test_an_interval_excluding_zero_explains_why_it_still_reads_no_change(self):
        base = [
            {"resolved": i < 100, "escalated": i < 100, "repeated_error": i < 100,
             "frustration_signal": i < 100, "baseline": True}
            for i in range(200)
        ]
        current = [
            {"resolved": i < 122, "escalated": i < 100, "repeated_error": i < 100,
             "frustration_signal": i < 100, "baseline": False}
            for i in range(200)
        ]
        row = _row(outcomes.compare(base, current), "resolution_rate")
        assert row["verdict"] == outcomes.VERDICT_INCONCLUSIVE
        assert row["ci_95"][0] > 0, "fixture should produce an interval excluding zero"
        assert "uncorrected" in row["note"]

    def test_cost_metrics_carry_no_p_value(self):
        report = outcomes.compare(
            [{"tokens_used": 1000, "baseline": True}], [{"tokens_used": 700, "baseline": False}]
        )
        row = next(r for r in report["cost"] if r["metric"] == "avg_tokens_used")
        assert row["delta"] == -300
        assert "p_value" not in row
        assert "significant" not in row


class TestAgainstPostgres:
    async def test_end_to_end_improvement(self, session_factory, orgs):
        await _traces(session_factory, orgs["fleet-a"], _arm(120, resolved_true=60, baseline=True))
        await _traces(session_factory, orgs["fleet-a"], _arm(120, resolved_true=96, baseline=False))

        report = await _report(session_factory, orgs["fleet-a"])
        assert report["n_baseline_traces"] == 120
        assert report["n_current_traces"] == 120
        assert _row(report, "resolution_rate")["verdict"] == outcomes.VERDICT_IMPROVED

    async def test_another_orgs_traces_never_enter_the_comparison(self, session_factory, orgs):
        await _traces(session_factory, orgs["fleet-a"], _arm(60, resolved_true=30, baseline=True))
        await _traces(session_factory, orgs["fleet-b"], _arm(500, resolved_true=500, baseline=False))

        report = await _report(session_factory, orgs["fleet-a"])
        assert report["n_traces"] == 60
        assert report["n_current_traces"] == 0

    async def test_quarantined_traces_are_excluded(self, session_factory, orgs):
        await _traces(session_factory, orgs["fleet-a"], _arm(40, resolved_true=20, baseline=True))
        await _traces(
            session_factory, orgs["fleet-a"], _arm(40, resolved_true=40, baseline=False),
            quarantined=True,
        )
        report = await _report(session_factory, orgs["fleet-a"])
        assert report["n_traces"] == 40
        assert report["n_current_traces"] == 0

    async def test_agent_type_narrows_the_comparison(self, session_factory, orgs):
        await _traces(
            session_factory, orgs["fleet-a"], _arm(40, resolved_true=20, baseline=True),
            agent_type="support",
        )
        await _traces(
            session_factory, orgs["fleet-a"], _arm(40, resolved_true=20, baseline=True),
            agent_type="code",
        )
        report = await _report(session_factory, orgs["fleet-a"], agent_type="support")
        assert report["n_traces"] == 40
        assert report["agent_type"] == "support"

    async def test_an_org_with_no_traces_reports_cleanly(self, session_factory, orgs):
        report = await _report(session_factory, orgs["fleet-a"])
        assert report["n_traces"] == 0
        assert "nothing to compare against" in report["headline"]
        assert all(r["verdict"] == outcomes.VERDICT_INSUFFICIENT for r in report["metrics"])

    async def test_a_malformed_outcome_column_does_not_crash_the_report(
        self, session_factory, orgs
    ):
        await _traces(
            session_factory, orgs["fleet-a"],
            [{"resolved": "yes", "baseline": "true"}, {"resolved": True, "baseline": True}],
        )
        report = await _report(session_factory, orgs["fleet-a"])
        assert report["n_traces"] == 2


class TestSqlAndPythonCountingAgree:
    async def test_identical_report_from_both_paths(self, session_factory, orgs):
        mixed = [
            {"resolved": True, "escalated": False, "repeated_error": True,
             "frustration_signal": False, "tokens_used": 900, "llm_calls": 4, "baseline": True},
            {"resolved": False, "escalated": True, "tokens_used": 1500, "baseline": True},
            {"resolved": None, "escalated": False, "baseline": True},
            {},
            {"resolved": "true", "tokens_used": True, "baseline": False},
            {"resolved": True, "escalated": False, "repeated_error": False,
             "frustration_signal": True, "tokens_used": 700, "llm_calls": 2, "baseline": False},
            {"resolved": True, "tokens_used": 1100, "baseline": False},
            {"resolved": False, "baseline": False},
        ]
        await _traces(session_factory, orgs["fleet-a"], mixed)

        from_sql = await _report(session_factory, orgs["fleet-a"])
        base, cur = outcomes.split_arms(mixed)
        from_python = outcomes.compare(base, cur)

        assert from_sql["n_baseline_traces"] == from_python["n_baseline_traces"]
        assert from_sql["n_current_traces"] == from_python["n_current_traces"]
        for a, b in zip(from_sql["metrics"], from_python["metrics"]):
            assert a["metric"] == b["metric"]
            assert a["baseline"] == b["baseline"], a["metric"]
            assert a["current"] == b["current"], a["metric"]
            assert a["verdict"] == b["verdict"], a["metric"]
        for a, b in zip(from_sql["cost"], from_python["cost"]):
            assert a["metric"] == b["metric"]
            assert a["baseline"] == pytest.approx(b["baseline"]), a["metric"]
            assert a["current"] == pytest.approx(b["current"]), a["metric"]

    async def test_both_paths_agree_that_a_stringified_bool_is_not_a_success(
        self, session_factory, orgs
    ):
        rows = [{"resolved": "true", "baseline": True}, {"resolved": "true", "baseline": False}]
        await _traces(session_factory, orgs["fleet-a"], rows)

        report = await _report(session_factory, orgs["fleet-a"])
        row = _row(report, "resolution_rate")
        assert row["baseline"] == {"rate": None, "n": 0}
        assert row["current"] == {"rate": None, "n": 0}


class TestManageOutcomes:
    async def test_reports_a_named_org(self, session_factory, orgs, capsys):
        await _traces(session_factory, orgs["fleet-a"], _arm(120, resolved_true=60, baseline=True))
        await _traces(session_factory, orgs["fleet-a"], _arm(120, resolved_true=96, baseline=False))

        assert await manage.fleet_outcomes(orgs["fleet-a"], session_factory=session_factory)
        out = capsys.readouterr().out
        assert "NOT A CAUSAL EFFECT" in out
        assert "resolution_rate" in out
        assert "improved" in out

    async def test_a_worsening_fleet_is_named_in_the_rollup(self, session_factory, orgs, capsys):
        await _traces(session_factory, orgs["fleet-a"], _arm(200, resolved_true=160, baseline=True))
        await _traces(session_factory, orgs["fleet-a"], _arm(200, resolved_true=100, baseline=False))

        assert await manage.fleet_outcomes(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "WORSENED" in out
        assert "moved backwards on something:  fleet-a" in out

    async def test_the_rollup_warns_about_scanning_many_orgs(self, session_factory, orgs, capsys):
        for org_id in orgs.values():
            await _traces(session_factory, org_id, _arm(60, resolved_true=30, baseline=True))
            await _traces(session_factory, org_id, _arm(60, resolved_true=36, baseline=False))

        assert await manage.fleet_outcomes(session_factory=session_factory)
        assert "multiple-comparisons" in capsys.readouterr().out

    async def test_an_unknown_org_fails_cleanly(self, session_factory, capsys):
        assert not await manage.fleet_outcomes(
            "00000000-0000-0000-0000-000000000000", session_factory=session_factory
        )
        assert "no such organization" in capsys.readouterr().err
