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
