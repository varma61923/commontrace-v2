"""Tests for the randomized holdout: the Hub's only causal instrument.

`hub/outcomes.py` compares a fleet against its own past and cannot rule
out anything else that changed in the same window. This compares two arms
of the same fleet in the same window, differing only by whether the memory
was injected -- so it survives "what else changed that quarter?", which is
the question that kills an observational number.

STRATEGY.md §11.3 names causally-measured memory as the entire moat, and
§13.2 calls running this "the cheapest falsifier in the document" and says
to run it first. Both were true of `commontrace/experiment.py`, which
works against a local file store; nothing in the Hub could do it, so the
falsifier could not be run on the surface paying customers are on.

What these tests defend, in order of how badly getting it wrong would
corrupt a result:

1. **Assignment is stable.** A retry must return the same arms. An
   occasion that moved between arms would not raise -- it would quietly
   contaminate the comparison.
2. **Arms are recorded exactly once**, so a retrying client cannot double
   an occasion's weight.
3. **Two experiments never pool.** Observations from a previous salt came
   from a different randomization.
4. **Missing outcomes are excluded, not counted as failures**, or the arm
   whose agents crash more looks worse for that reason alone.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy import update as sa_update

from commontrace import revision
from hub import crud, manage
from hub.db import session_scope
from hub.models import HoldoutObservation, Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="fleet", holdout_rate=0.5, holdout_salt="salt-one")
        session.add(o)
        await session.flush()
        return o.id


@pytest_asyncio.fixture
async def other_org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="other", holdout_rate=0.5, holdout_salt="salt-other")
        session.add(o)
        await session.flush()
        return o.id


async def _traces(session_factory, org_id, n):
    ids = []
    async with session_scope(session_factory) as session:
        for i in range(n):
            t = Trace(
                org_id=org_id, title=f"lesson {i}", context_text="c",
                solution_text="s", tags=[], agent_type="code",
            )
            session.add(t)
            await session.flush()
            ids.append(t.id)
    return ids


async def _assign(session_factory, org_id, trace_ids, occasion):
    async with session_scope(session_factory) as session:
        return await crud.holdout_assign(session, org_id, trace_ids, occasion)


async def _resolve(session_factory, org_id, occasion, succeeded):
    async with session_scope(session_factory) as session:
        return await crud.record_occasion_outcome(session, org_id, occasion, succeeded)


class TestAssignment:
    async def test_both_arms_are_produced_over_many_occasions(self, session_factory, org):
        """At a 50% rate, a run of occasions must land in both arms. A
        randomizer that silently always returned one arm would look like a
        working experiment right up until the analysis reported
        UNDERPOWERED forever."""
        [trace] = await _traces(session_factory, org, 1)
        arms = set()
        for i in range(40):
            result = await _assign(session_factory, org, [trace], f"occ-{i}")
            arms.add(bool(result["inject"]))
        assert arms == {True, False}

    async def test_the_same_call_twice_returns_the_same_arms(self, session_factory, org):
        """The property a retrying client depends on. Assignment is a pure
        hash of (salt, trace, occasion), so a timeout-and-retry cannot move
        an occasion between arms."""
        traces = await _traces(session_factory, org, 6)
        first = await _assign(session_factory, org, traces, "occ-1")
        second = await _assign(session_factory, org, traces, "occ-1")
        assert first["inject"] == second["inject"]
        assert first["withhold"] == second["withhold"]

    async def test_a_retry_records_no_duplicate_rows(self, session_factory, org):
        """Assignment being deterministic means a duplicate row would land
        in the SAME arm -- so it would not look wrong, it would just double
        that occasion's weight in the result."""
        traces = await _traces(session_factory, org, 4)
        for _ in range(3):
            await _assign(session_factory, org, traces, "occ-1")
        async with session_scope(session_factory) as session:
            n = await session.scalar(
                select(func.count()).select_from(HoldoutObservation)
                .where(HoldoutObservation.occasion_id == "occ-1")
            )
        assert n == 4

    async def test_two_lessons_land_in_uncorrelated_arms(self, session_factory, org):
        """Independence per trace is what makes individual effects
        separable. Two traces that always co-fire must not always share an
        arm, or their effects are hopelessly confounded."""
        a, b = await _traces(session_factory, org, 2)
        together = 0
        for i in range(60):
            r = await _assign(session_factory, org, [a, b], f"occ-{i}")
            if len(r["inject"]) in (0, 2):
                together += 1
        # Perfectly correlated would be 60; independent is ~30.
        assert 10 < together < 50, together

    async def test_another_orgs_trace_is_silently_absent(self, session_factory, org, other_org):
        mine = await _traces(session_factory, org, 1)
        theirs = await _traces(session_factory, other_org, 1)
        result = await _assign(session_factory, org, mine + theirs, "occ-1")
        assert set(result["inject"]) | set(result["withhold"]) == set(mine)

    async def test_no_experiment_running_is_a_clean_error(self, session_factory):
        """Not a silent 'inject everything': a client that believes it is
        running an experiment and is not would produce an all-injected
        dataset that reads as underpowered rather than as misconfigured."""
        async with session_scope(session_factory) as session:
            o = Organization(name="no-experiment")
            session.add(o)
            await session.flush()
            org_id = o.id
        trace = (await _traces(session_factory, org_id, 1))[0]
        async with session_scope(session_factory) as session:
            with pytest.raises(crud.ExperimentNotRunning):
                await crud.holdout_assign(session, org_id, [trace], "occ-1")

    async def test_a_missing_occasion_id_is_refused(self, session_factory, org):
        trace = (await _traces(session_factory, org, 1))[0]
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="occasion_id is required"):
                await crud.holdout_assign(session, org, [trace], "  ")

    async def test_an_oversized_batch_is_refused(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="too many traces"):
                await crud.holdout_assign(
                    session, org, ["x"] * (crud.MAX_TRACES_PER_ASSIGN + 1), "occ-1"
                )

    async def test_a_non_list_trace_ids_is_refused(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="trace_ids must be a list"):
                await crud.holdout_assign(session, org, "not-a-list", "occ-1")

    async def test_an_oversized_occasion_id_is_refused(self, session_factory, org):
        trace = (await _traces(session_factory, org, 1))[0]
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="occasion_id exceeds"):
                await crud.holdout_assign(session, org, [trace], "x" * (crud.MAX_OCCASION_ID_CHARS + 1))


class TestSearchIntegration:
    """The friction that decides whether the experiment ever runs.

    The local tier makes a holdout one flag (`query --experiment`):
    retrieval withholds and logs, so a fleet opts in without rewriting an
    agent's loop. Requiring two extra explicit calls around every Hub
    retrieval is a rewrite, and STRATEGY.md §13.2 calls running this the
    cheapest falsifier available -- so friction here is not a UX detail.
    """

    async def test_no_occasion_id_leaves_search_completely_unchanged(
        self, session_factory, org
    ):
        """Backward compatibility is the whole reason this is an optional
        parameter: every existing caller must see identical behaviour."""
        await _traces(session_factory, org, 3)
        async with session_scope(session_factory) as session:
            result = await crud.search_traces(session, org, query="lesson")
        assert "holdout" not in result

    async def test_an_occasion_id_adds_the_withhold_list(self, session_factory, org):
        traces = await _traces(session_factory, org, 12)
        async with session_scope(session_factory) as session:
            found = await crud.search_traces(session, org, limit=50)
            holdout = await crud.holdout_for_results(
                session, org, found["traces"], "occ-search-1"
            )
        assert holdout["occasion_id"] == "occ-search-1"
        assert set(holdout["withhold"]) <= set(traces)
        assert "must NOT be used" in holdout["note"]

    async def test_every_trace_is_still_returned(self, session_factory, org):
        """A withheld trace is flagged, never omitted. Silently dropping
        results would break search_traces' contract (PROTOCOL.md §5) and
        make the experiment invisible to a caller who ignores the block."""
        traces = await _traces(session_factory, org, 10)
        async with session_scope(session_factory) as session:
            found = await crud.search_traces(session, org, limit=50)
            holdout = await crud.holdout_for_results(
                session, org, found["traces"], "occ-search-2"
            )
        assert len(found["traces"]) == len(traces)
        assert holdout["withhold"]

    async def test_it_records_observations_the_analysis_can_use(self, session_factory, org):
        await _traces(session_factory, org, 6)
        async with session_scope(session_factory) as session:
            found = await crud.search_traces(session, org, limit=50)
            await crud.holdout_for_results(session, org, found["traces"], "occ-search-3")
        await _resolve(session_factory, org, "occ-search-3", True)
        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)
        assert report["n_observations"] == 6

    async def test_no_experiment_running_returns_empty_rather_than_raising(
        self, session_factory
    ):
        """Quieter than holdout_assign on purpose: a caller of THAT tool
        explicitly asked to run an experiment and should be told it is off.
        A caller of search_traces only asked to search, so a passed-through
        occasion_id must not turn an ordinary search into an error."""
        async with session_scope(session_factory) as session:
            o = Organization(name="no-experiment")
            session.add(o)
            await session.flush()
            org_id = o.id
        await _traces(session_factory, org_id, 2)
        async with session_scope(session_factory) as session:
            found = await crud.search_traces(session, org_id, limit=50)
            assert await crud.holdout_for_results(
                session, org_id, found["traces"], "occ-1"
            ) == {}

    async def test_an_empty_result_set_records_nothing(self, session_factory, org):
        async with session_scope(session_factory) as session:
            assert await crud.holdout_for_results(session, org, [], "occ-1") == {}


class TestRecordingOutcomes:
    async def test_an_outcome_resolves_both_arms_at_once(self, session_factory, org):
        """The outcome belongs to the TASK, not to any one memory."""
        traces = await _traces(session_factory, org, 8)
        await _assign(session_factory, org, traces, "occ-1")
        result = await _resolve(session_factory, org, "occ-1", True)
        assert result["observations_resolved"] == 8

    async def test_a_second_report_cannot_flip_a_counted_result(self, session_factory, org):
        """Otherwise a retry loop could walk a result back and forth, and
        the analysis would depend on which call happened to land last."""
        traces = await _traces(session_factory, org, 3)
        await _assign(session_factory, org, traces, "occ-1")
        await _resolve(session_factory, org, "occ-1", True)
        second = await _resolve(session_factory, org, "occ-1", False)
        assert second["observations_resolved"] == 0

        async with session_scope(session_factory) as session:
            rows = (
                await session.execute(
                    select(HoldoutObservation.succeeded).where(
                        HoldoutObservation.occasion_id == "occ-1"
                    )
                )
            ).scalars().all()
        assert all(r is True for r in rows)

    async def test_another_orgs_occasion_is_untouched(self, session_factory, org, other_org):
        mine = await _traces(session_factory, org, 2)
        theirs = await _traces(session_factory, other_org, 2)
        await _assign(session_factory, org, mine, "shared-name")
        await _assign(session_factory, other_org, theirs, "shared-name")

        assert (await _resolve(session_factory, org, "shared-name", True))[
            "observations_resolved"
        ] == 2
        async with session_scope(session_factory) as session:
            unresolved = await session.scalar(
                select(func.count()).select_from(HoldoutObservation).where(
                    HoldoutObservation.org_id == other_org,
                    HoldoutObservation.succeeded.is_(None),
                )
            )
        assert unresolved == 2

    async def test_a_non_boolean_outcome_is_refused(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="must be a boolean"):
                await crud.record_occasion_outcome(session, org, "occ-1", "yes")

    async def test_a_missing_occasion_id_is_refused(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="occasion_id is required"):
                await crud.record_occasion_outcome(session, org, "  ", True)


class TestAnalysis:
    async def _run_experiment(self, session_factory, org, trace, n, p_injected, p_withheld):
        """Drive `n` occasions through the real assign/record path with a
        seeded difference between the arms."""
        injected_seen = withheld_seen = 0
        for i in range(n):
            occ = f"occ-{i}"
            result = await _assign(session_factory, org, [trace], occ)
            if result["inject"]:
                injected_seen += 1
                ok = (injected_seen % 100) < int(p_injected * 100)
            else:
                withheld_seen += 1
                ok = (withheld_seen % 100) < int(p_withheld * 100)
            await _resolve(session_factory, org, occ, ok)

    async def test_a_real_effect_is_recovered_as_helps(self, session_factory, org):
        """Seeded at 80% injected vs 40% withheld."""
        [trace] = await _traces(session_factory, org, 1)
        await self._run_experiment(session_factory, org, trace, 400, 0.80, 0.40)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)
        effect = report["effects"][0]
        assert effect["verdict"] == "HELPS"
        assert effect["effect"] > 0.2
        assert effect["ci_95"][0] > 0
        assert effect["title"] == "lesson 0"

    async def test_a_harmful_lesson_is_reported_as_hurts(self, session_factory, org):
        """The direction that matters most: an instrument that cannot say
        a lesson makes things worse cannot credibly say one helps."""
        [trace] = await _traces(session_factory, org, 1)
        await self._run_experiment(session_factory, org, trace, 400, 0.35, 0.75)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)
        assert report["effects"][0]["verdict"] == "HURTS"

    async def test_too_few_occasions_reads_as_underpowered_not_no_effect(
        self, session_factory, org
    ):
        [trace] = await _traces(session_factory, org, 1)
        await self._run_experiment(session_factory, org, trace, 8, 0.9, 0.3)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)
        effect = report["effects"][0]
        assert effect["verdict"] == "UNDERPOWERED"
        assert "cannot answer yet" in effect["note"]

    async def test_unresolved_observations_are_excluded_not_counted_as_failures(
        self, session_factory, org
    ):
        """An agent that crashed before reporting is missing data. Scoring
        it as a loss would bias whichever arm crashed more."""
        [trace] = await _traces(session_factory, org, 1)
        for i in range(20):
            await _assign(session_factory, org, [trace], f"occ-{i}")
        await _resolve(session_factory, org, "occ-0", True)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)
        assert report["n_observations"] == 1

    async def test_observations_from_a_previous_salt_are_not_pooled(
        self, session_factory, org, capsys
    ):
        """Two experiments are two randomizations. Pooling them compares
        two mixtures and biases the effect toward zero -- and it would not
        look broken, it would look like a null result."""
        [trace] = await _traces(session_factory, org, 1)
        await self._run_experiment(session_factory, org, trace, 60, 0.9, 0.3)
        async with session_scope(session_factory) as session:
            before = await crud.causal_effects(session, org)
        assert before["n_observations"] == 60

        assert await manage.start_experiment(org, "0.5", session_factory=session_factory)
        capsys.readouterr()

        async with session_scope(session_factory) as session:
            after = await crud.causal_effects(session, org)
        assert after["n_observations"] == 0

    async def test_an_org_with_no_experiment_reports_cleanly(self, session_factory):
        async with session_scope(session_factory) as session:
            o = Organization(name="quiet")
            session.add(o)
            await session.flush()
            report = await crud.causal_effects(session, o.id)
        assert report["experiment_running"] is False
        assert report["effects"] == []


class TestOperatorCommands:
    async def test_start_sets_a_rate_and_a_fresh_salt(self, session_factory, capsys):
        async with session_scope(session_factory) as session:
            o = Organization(name="fleet")
            session.add(o)
            await session.flush()
            org_id = o.id

        assert await manage.start_experiment(org_id, "0.25", session_factory=session_factory)
        out = capsys.readouterr().out
        assert "25%" in out

        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            assert org.holdout_rate == 0.25
            assert org.holdout_salt

    async def test_restarting_generates_a_different_salt(self, session_factory, capsys):
        async with session_scope(session_factory) as session:
            o = Organization(name="fleet")
            session.add(o)
            await session.flush()
            org_id = o.id

        await manage.start_experiment(org_id, "0.2", session_factory=session_factory)
        async with session_scope(session_factory) as session:
            first = (await session.get(Organization, org_id)).holdout_salt
        await manage.start_experiment(org_id, "0.2", session_factory=session_factory)
        async with session_scope(session_factory) as session:
            second = (await session.get(Organization, org_id)).holdout_salt
        assert first != second
        assert "replaces experiment" in capsys.readouterr().out

    async def test_a_rate_outside_zero_to_one_is_refused(self, session_factory, org, capsys):
        """0 withholds nothing (no control arm); 1 withholds everything (no
        treatment arm). Neither is an experiment."""
        for bad in ("0", "1", "1.5", "-0.2"):
            assert not await manage.start_experiment(org, bad, session_factory=session_factory)
            assert "between 0 and 1" in capsys.readouterr().err

    async def test_a_non_numeric_rate_is_refused(self, session_factory, org, capsys):
        assert not await manage.start_experiment(org, "half", session_factory=session_factory)
        assert "must be a number" in capsys.readouterr().err

    async def test_stop_halts_withholding_but_keeps_observations(
        self, session_factory, org, capsys
    ):
        traces = await _traces(session_factory, org, 2)
        await _assign(session_factory, org, traces, "occ-1")
        await _resolve(session_factory, org, "occ-1", True)

        assert await manage.stop_experiment(org, session_factory=session_factory)
        capsys.readouterr()
        async with session_scope(session_factory) as session:
            assert (await session.get(Organization, org)).holdout_rate == 0.0
            report = await crud.causal_effects(session, org)
        assert report["experiment_running"] is False
        assert report["n_observations"] == 2

    async def test_stopping_a_stopped_experiment_says_so(self, session_factory, capsys):
        async with session_scope(session_factory) as session:
            o = Organization(name="idle")
            session.add(o)
            await session.flush()
            org_id = o.id
        assert not await manage.stop_experiment(org_id, session_factory=session_factory)
        assert "No experiment is running" in capsys.readouterr().err

    async def test_experiment_report_prints_the_verdicts(self, session_factory, org, capsys):
        [trace] = await _traces(session_factory, org, 1)
        for i in range(120):
            occ = f"occ-{i}"
            result = await _assign(session_factory, org, [trace], occ)
            await _resolve(session_factory, org, occ, bool(result["inject"]) or i % 5 == 0)

        assert await manage.experiment_results(org, session_factory=session_factory)
        out = capsys.readouterr().out
        assert "lesson 0" in out
        assert "injected" in out and "withheld" in out

    async def test_experiment_on_an_unknown_org_fails_cleanly(self, session_factory, capsys):
        assert not await manage.experiment_results(
            "00000000-0000-0000-0000-000000000000", session_factory=session_factory
        )
        assert "no such organization" in capsys.readouterr().err


class TestTheEstimateIsAuditable:
    """Excluding unresolved observations (point 4 above) is correct handling
    and, on its own, not enough.

    Dropping them is unbiased ONLY if both arms lose them at the same rate.
    The withheld arm is by construction the one working without its memory,
    so it is the arm more likely to run long, escalate, or be abandoned
    before anyone reports -- the treatment effect leaking into who gets
    measured. `causal_effects` used to filter `succeeded IS NOT NULL` in the
    SQL itself, which meant nothing downstream could even count what was
    missing, let alone which arm it came from.

    `tests/test_integrity.py` shows what that costs: a lesson with no effect
    at all reporting a significant verdict with a tight interval.
    """

    async def test_the_report_carries_a_validity_verdict(self, session_factory, org):
        # 80 occasions rather than 40: assignment hashes a random trace uuid,
        # so the realized split differs run to run and a marginal sample made
        # this assertion depend on the draw.
        traces = await _traces(session_factory, org, 1)
        for i in range(80):
            await _assign(session_factory, org, traces, f"occ-{i}")
            await _resolve(session_factory, org, f"occ-{i}", i % 3 == 0)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)

        assert report["integrity"]["verdict"] == "SOUND", report["integrity"]["findings"]
        assert report["integrity"]["effects_readable"] is True
        assert report["integrity"]["n_resolved"] == 80

    async def test_passing_checks_come_back_too_not_just_failures(self, session_factory, org):
        """A caller cannot tell "checked, clean" from "not checked" when only
        problems are reported, and those mean opposite things about how far
        to trust the number underneath."""
        traces = await _traces(session_factory, org, 1)
        for i in range(30):
            await _assign(session_factory, org, traces, f"occ-{i}")
            await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)

        checks = {f["check"] for f in report["integrity"]["findings"]}
        assert {"differential_attrition", "arm_balance", "consistent_arms"} <= checks

    async def test_a_reporting_gap_in_one_arm_is_caught_and_named(self, session_factory, org):
        """The load-bearing case, built the way it actually happens: every
        injected occasion gets reported and most withheld ones do not."""
        traces = await _traces(session_factory, org, 1)
        for i in range(120):
            result = await _assign(session_factory, org, traces, f"occ-{i}")
            injected = bool(result["inject"])
            if injected or i % 4 == 0:
                await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)

        integrity_report = report["integrity"]
        assert integrity_report["verdict"] == "COMPROMISED"
        assert integrity_report["effects_readable"] is False
        blocking = [f for f in integrity_report["findings"] if f["severity"] == "INVALIDATES"]
        assert [f["check"] for f in blocking] == ["differential_attrition"]
        # The unresolved rows are visible now. Before this they were filtered
        # out in SQL, so `n_assignments` and `n_resolved` were the same number
        # and the gap could not be seen from the response at all.
        assert integrity_report["n_assignments"] > integrity_report["n_resolved"]

    async def test_a_compromised_report_says_so_in_the_note_an_agent_reads(
        self, session_factory, org
    ):
        """The MCP response is read by an agent, which acts on whichever field
        it looks at first. The note that ships beside `effects` has to carry
        the warning, not only a sibling key it may never open."""
        traces = await _traces(session_factory, org, 1)
        for i in range(120):
            result = await _assign(session_factory, org, traces, f"occ-{i}")
            if bool(result["inject"]) or i % 4 == 0:
                await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)

        assert "READ `integrity` FIRST" in report["note"]

    async def test_it_projects_when_each_trace_becomes_answerable(self, session_factory, org):
        """`experiment` already says a lesson is underpowered. What decides
        whether a pilot lands is WHEN -- told on day 30 the pilot is spent,
        told on day 3 the holdout rate is still changeable."""
        traces = await _traces(session_factory, org, 1)
        for i in range(20):
            await _assign(session_factory, org, traces, f"occ-{i}")
            await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)

        [projection] = report["integrity"]["projections"]
        assert projection["trace_id"] == traces[0]
        assert projection["binding_arm"] in ("withheld", "injected")
        assert projection["advice"]

    async def test_it_states_what_it_cannot_check(self, session_factory, org):
        """Contamination -- an agent using a trace it was told to withhold --
        leaves no trace in the record. Silence about it would read as
        coverage of a failure nothing here can see."""
        traces = await _traces(session_factory, org, 1)
        await _assign(session_factory, org, traces, "occ-1")
        await _resolve(session_factory, org, "occ-1", True)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)

        assert "withhold" in report["integrity"]["not_checkable"]

    async def test_two_experiments_still_never_pool(self, session_factory, org):
        """The salt scope is still applied in SQL. Removing the
        `succeeded IS NOT NULL` filter must not have widened anything else."""
        traces = await _traces(session_factory, org, 1)
        for i in range(10):
            await _assign(session_factory, org, traces, f"occ-{i}")
            await _resolve(session_factory, org, f"occ-{i}", True)

        async with session_scope(session_factory) as session:
            organization = await session.get(Organization, org)
            organization.holdout_salt = "salt-two"

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)

        assert report["integrity"]["n_assignments"] == 0
        assert report["effects"] == []


class TestTheTreatmentIsPinnedToItsText:
    """`trace_id` is a stable id pointing at MUTABLE content: `amend_trace`
    rewrites title, context and solution in place.

    So an observation recording only the id cannot tell whether every
    occasion in an arm was treated with the same text -- and when they were
    not, the pooled effect describes a treatment that is an average of two,
    one of which no longer exists anywhere. This is the same defect
    `Organization.holdout_salt` exists to make detectable, one level down.
    """

    async def test_assignment_records_what_the_trace_said(self, session_factory, org):
        traces = await _traces(session_factory, org, 1)
        await _assign(session_factory, org, traces, "occ-1")

        async with session_scope(session_factory) as session:
            row = (await session.execute(
                select(HoldoutObservation).where(HoldoutObservation.org_id == org)
            )).scalars().one()
            trace = await session.get(Trace, traces[0])
            assert row.trace_revision == revision.revision_of_trace(
                trace.title, trace.context_text, trace.solution_text, list(trace.tags or [])
            )

    async def test_amending_a_trace_mid_experiment_is_caught(self, session_factory, org):
        """The realistic version: a trace is corrected part-way through a run,
        which is an ordinary and good thing to do -- and silently makes the
        two halves of the experiment different experiments."""
        traces = await _traces(session_factory, org, 1)
        for i in range(20):
            await _assign(session_factory, org, traces, f"occ-{i}")
            await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)

        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, traces[0])
            trace.solution_text = "A materially different solution, rewritten mid-run."

        for i in range(20, 40):
            await _assign(session_factory, org, traces, f"occ-{i}")
            await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)

        assert report["integrity"]["verdict"] == "COMPROMISED"
        blocking = [f["check"] for f in report["integrity"]["findings"]
                    if f["severity"] == "INVALIDATES"]
        assert "treatment_stability" in blocking

    async def test_an_unchanged_trace_reads_as_stable(self, session_factory, org):
        traces = await _traces(session_factory, org, 1)
        for i in range(20):
            await _assign(session_factory, org, traces, f"occ-{i}")
            await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)

        stability = next(f for f in report["integrity"]["findings"]
                         if f["check"] == "treatment_stability")
        assert stability["severity"] == "OK"
        assert report["integrity"]["verdict"] == "SOUND"

    async def test_recording_an_outcome_is_not_a_change_to_the_treatment(
        self, session_factory, org
    ):
        """Outcomes attach AFTER retrieval by definition. Counting them would
        make every measured trace look edited, which is the false positive
        that teaches people to ignore a validity report."""
        traces = await _traces(session_factory, org, 1)
        for i in range(20):
            await _assign(session_factory, org, traces, f"occ-{i}")

        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, traces[0])
            trace.outcome = {"resolved": True, "tokens_used": 900}

        for i in range(20, 40):
            await _assign(session_factory, org, traces, f"occ-{i}")
            await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)
        for i in range(20):
            await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)

        stability = next(f for f in report["integrity"]["findings"]
                         if f["check"] == "treatment_stability")
        assert stability["severity"] == "OK", stability

    async def test_a_retry_does_not_restamp_the_revision(self, session_factory, org):
        """ON CONFLICT DO NOTHING: the first assignment's revision is what
        that occasion was treated with. A retry after an edit must not
        rewrite history to say otherwise."""
        traces = await _traces(session_factory, org, 1)
        await _assign(session_factory, org, traces, "occ-1")

        async with session_scope(session_factory) as session:
            original = (await session.execute(
                select(HoldoutObservation.trace_revision).where(
                    HoldoutObservation.org_id == org)
            )).scalars().one()
            trace = await session.get(Trace, traces[0])
            trace.solution_text = "Rewritten after the assignment was made."

        await _assign(session_factory, org, traces, "occ-1")

        async with session_scope(session_factory) as session:
            rows = (await session.execute(
                select(HoldoutObservation.trace_revision).where(
                    HoldoutObservation.org_id == org)
            )).scalars().all()
        assert rows == [original]

    async def test_rows_written_before_the_column_existed_are_unchecked(
        self, session_factory, org
    ):
        """NULL is never backfilled: what a trace said at assignment time is
        unrecoverable once it has been amended, and stamping today's digest
        would assert stability on exactly the runs where nobody can know."""
        traces = await _traces(session_factory, org, 1)
        for i in range(20):
            await _assign(session_factory, org, traces, f"occ-{i}")
            await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)

        async with session_scope(session_factory) as session:
            await session.execute(
                sa_update(HoldoutObservation)
                .where(HoldoutObservation.org_id == org)
                .values(trace_revision=None)
            )

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)

        stability = next(f for f in report["integrity"]["findings"]
                         if f["check"] == "treatment_stability")
        assert stability["severity"] == "WEAKENS"
        assert "cannot be checked" in stability["headline"]


class TestWhatTheMemoryWasWorth:
    """STRATEGY.md §11.5 names the pricing hypothesis this product rests on --
    price against measured effect per fleet, not seats or trace volume -- and
    says the mechanism ships. Half of that was true: the effect size shipped,
    and this file's own surface computed no value at all.

    These check the rules that keep the resulting figure a measurement.
    """

    async def _running_experiment(self, session_factory, org, n=120, helps=True):
        traces = await _traces(session_factory, org, 1)
        for i in range(n):
            result = await _assign(session_factory, org, traces, f"occ-{i}")
            injected = bool(result["inject"])
            # A real effect, in the direction asked for.
            good = (i % 10 < 8) if (injected == helps) else (i % 10 < 3)
            await _resolve(session_factory, org, f"occ-{i}", good)
        return traces

    async def test_it_reports_occasions_and_takes_the_rate_from_the_caller(
        self, session_factory, org
    ):
        await self._running_experiment(session_factory, org)
        async with session_scope(session_factory) as session:
            counted = await crud.value_delivered(session, org)
            priced = await crud.value_delivered(session, org, value_per_occasion=25.0)

        assert counted["readable"]
        # A count on its own; currency only once a rate is supplied, and no
        # price is stored anywhere.
        assert counted["money"] is None
        assert counted["value_per_occasion"] is None
        assert priced["money"] == pytest.approx(
            priced["occasions_improved"] * 25.0, rel=1e-6)
        assert priced["money_range"] is not None

    async def test_a_compromised_experiment_yields_no_figure(self, session_factory, org):
        """Every injected occasion reported, most withheld ones not -- the
        attrition case. The effects are biased, so any value computed from
        them is biased by the same mechanism."""
        traces = await _traces(session_factory, org, 1)
        for i in range(120):
            result = await _assign(session_factory, org, traces, f"occ-{i}")
            if bool(result["inject"]) or i % 4 == 0:
                await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)

        async with session_scope(session_factory) as session:
            worth = await crud.value_delivered(session, org, value_per_occasion=25.0)

        assert worth["readable"] is False
        assert worth["occasions_improved"] == 0.0
        assert worth["money"] is None
        assert "COMPROMISED" in worth["reason"]

    async def test_the_value_and_the_effect_table_cannot_disagree(
        self, session_factory, org
    ):
        """It reads `causal_effects` rather than re-querying, so the two
        surfaces of the same run are always computed from one set of rows."""
        await self._running_experiment(session_factory, org)
        async with session_scope(session_factory) as session:
            causal = await crud.causal_effects(session, org)
            worth = await crud.value_delivered(session, org)

        assert {m["trace_id"] for m in worth["memories"]} == \
            {e["trace_id"] for e in causal["effects"]}
        for memory in worth["memories"]:
            match = next(e for e in causal["effects"] if e["trace_id"] == memory["trace_id"])
            assert memory["verdict"] == match["verdict"]
            assert memory["n_injected"] == match["n_injected"]

    async def test_a_memory_that_hurts_is_subtracted(self, session_factory, org):
        """The number this product must be willing to print about itself."""
        await self._running_experiment(session_factory, org, helps=False)
        async with session_scope(session_factory) as session:
            causal = await crud.causal_effects(session, org)
            worth = await crud.value_delivered(session, org)

        hurts = [e for e in causal["effects"] if e["verdict"] == "HURTS"]
        if not hurts:  # pragma: no cover - depends on the randomizer's split
            pytest.skip("this seed did not produce an adequately-powered HURTS verdict")
        assert worth["occasions_improved"] < 0
        counted = [m for m in worth["memories"] if m["verdict"] == "HURTS"]
        assert counted and all(m["counted"] for m in counted)

    async def test_it_states_where_the_number_comes_from(self, session_factory, org):
        await self._running_experiment(session_factory, org)
        async with session_scope(session_factory) as session:
            worth = await crud.value_delivered(session, org)
        assert "MEASURED" in worth["note"]
        assert "subtracted rather than" in worth["note"]

    async def test_it_never_reaches_another_orgs_data(
        self, session_factory, org, other_org
    ):
        await self._running_experiment(session_factory, org)
        async with session_scope(session_factory) as session:
            theirs = await crud.value_delivered(session, other_org)
        assert theirs["memories"] == []
        assert theirs["occasions_improved"] == 0.0
