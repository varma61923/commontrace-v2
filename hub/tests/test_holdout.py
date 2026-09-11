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

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy import update as sa_update

from commontrace import experiment, revision, value
from hub import crud, manage, plans
from hub.abuse import make_rate_limiter
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


# Genuinely unrelated incident topics, not "topic {i}": crud.holdout_for_
# results clusters near-duplicate search results (hub/crud.py:
# _cluster_representatives), and a shared boilerplate sentence differing
# only by an appended index number still shares enough vocabulary to
# cluster under Jaccard similarity -- these traces need to share close to
# NO tokens with each other, the same way distinct real lessons would, so
# `_traces(..., n)` continues to produce `n` INDEPENDENT randomization
# units rather than collapsing into one.
_UNRELATED_TOPICS = [
    "database connection pool exhaustion under concurrent load",
    "stale cache serving expired pricing data to checkout",
    "race condition in webhook retry backoff logic",
    "memory leak in long running background worker process",
    "certificate expiry breaking outbound TLS handshake calls",
    "deadlock between two concurrent schema migration jobs",
    "unicode normalization breaking full text search index",
    "clock skew causing signed token validation failures",
    "orphaned child rows after cascading delete bug",
    "N plus one query pattern degrading dashboard latency",
    "flaky integration test caused by shared fixture state",
    "incorrect timezone conversion in scheduled report export",
]


async def _traces(session_factory, org_id, n):
    if n > len(_UNRELATED_TOPICS):
        raise ValueError(f"only {len(_UNRELATED_TOPICS)} unrelated topics available, got n={n}")
    ids = []
    async with session_scope(session_factory) as session:
        for i in range(n):
            topic = _UNRELATED_TOPICS[i]
            t = Trace(
                org_id=org_id, title=f"lesson {i}",
                context_text=topic,
                solution_text=f"resolved by addressing {topic}",
                tags=[], agent_type="code",
            )
            session.add(t)
            await session.flush()
            ids.append(t.id)
    return ids


async def _assign(session_factory, org_id, trace_ids, occasion):
    async with session_scope(session_factory) as session:
        return await crud.holdout_assign(session, org_id, trace_ids, occasion)


async def _assign_pinned(session_factory, org_id, trace_ids, occasion, pinned):
    async with session_scope(session_factory) as session:
        return await crud.holdout_assign(
            session, org_id, trace_ids, occasion, pinned=pinned
        )


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


class TestNearDuplicateClustering:
    """A fleet's own habit of contributing a trace of what happened after
    each occasion creates one new, independent trace id per occasion that
    is a near-duplicate of whatever lesson it resolved. Without clustering,
    each duplicate accumulates its own handful of holdout observations
    instead of one lesson's observations accumulating on one id -- the
    shape that keeps a real, large effect UNDERPOWERED forever."""

    async def _near_duplicates(self, session_factory, config, org_id, n, *, failed_ids=()):
        """`n` traces that are all near-duplicates of ONE lesson (shared
        vocabulary, varying only the trailing occasion number), the shape
        `commontrace capture`-style self-logging actually produces."""
        rate_limiter = make_rate_limiter(config)
        ids = []
        async with session_scope(session_factory) as session:
            for i in range(n):
                outcome = {"resolved": False} if i in failed_ids else None
                result = await crud.contribute_trace(
                    session, org_id, config, rate_limiter,
                    title=f"connection pool exhausted (occasion {i})",
                    context_text="connection pool exhausted running the test suite in parallel",
                    solution_text="dispose the sqlalchemy engine in the fixture teardown",
                    tags=[], agent_type="code", outcome=outcome,
                )
                ids.append(result["id"])
        return ids

    async def test_near_duplicates_share_one_holdout_decision(self, session_factory, config, org):
        """The core property: all near-duplicate results in one search page
        resolve to the SAME withhold/inject verdict, because they are the
        same randomization unit underneath -- not `n` independent coin
        flips that would only agree by chance."""
        await self._near_duplicates(session_factory, config, org, 8)
        async with session_scope(session_factory) as session:
            found = await crud.search_traces(session, org, limit=50)
            holdout = await crud.holdout_for_results(session, org, found["traces"], "occ-1")
        withheld = set(holdout["withhold"])
        all_ids = {t["id"] for t in found["traces"]}
        # Either every one of them is withheld, or none of them are --
        # never a split, which independent per-id coin flips would produce
        # with overwhelming probability at n=8.
        assert withheld == all_ids or withheld == set()

    async def test_observations_accumulate_on_one_trace_id(self, session_factory, config, org):
        """This is the statistical-power fix, made concrete: 20 near-
        duplicate occasions produce ONE trace id with ~20 observations in
        the causal analysis, not 20 trace ids with ~1 each."""
        await self._near_duplicates(session_factory, config, org, 20)
        async with session_scope(session_factory) as session:
            found = await crud.search_traces(session, org, limit=50)
            for i in range(20):
                await crud.holdout_for_results(session, org, found["traces"], f"occ-{i}")
        for i in range(20):
            await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)
        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)
        assert report["n_observations"] == 20
        assert len(report["effects"]) == 1, (
            "20 near-duplicate injections should attribute to one trace id, "
            f"not fragment across {len(report['effects'])}"
        )

    async def test_the_unit_is_stable_as_the_page_composition_changes(
        self, session_factory, config, org
    ):
        """The failure the `limit=50` tests above structurally cannot see.

        `holdout_for_results` only ever sees ONE SEARCH PAGE, not a
        cluster's true membership -- so any representative rule that is a
        function of which members share that page drifts as the fleet logs
        more occasions and the page composition shifts, re-creating the
        exact fragmentation the clustering exists to remove, one level up.
        Measured on the audit's own dynamics before the fix: 17 distinct
        randomization units for a single lesson.

        A realistic page (limit=5) over a corpus that grows past it is the
        only shape that exercises this.
        """
        canonical = (await self._near_duplicates(session_factory, config, org, 1))[0]
        for i in range(12):
            async with session_scope(session_factory) as session:
                found = await crud.search_traces(session, org, query="connection pool exhausted", limit=5)
                await crud.holdout_for_results(session, org, found["traces"], f"occ-{i}")
            # The fleet logs what happened, growing the corpus past one page.
            await self._near_duplicates(session_factory, config, org, 1)

        async with session_scope(session_factory) as session:
            units = (
                await session.execute(select(func.distinct(HoldoutObservation.trace_id)))
            ).scalars().all()
        assert [str(u) for u in units] == [canonical], (
            f"one lesson must stay one randomization unit across changing pages; "
            f"got {len(units)} units"
        )

    async def test_representative_prefers_a_non_failed_member(self, session_factory, config, org):
        """The one id a cluster's observations get attributed to should not
        be an unresolved, escalated occasion log when a better-standing
        member of the same cluster exists -- that id's title is what a
        customer reading `causal_effects`/`value_delivered` sees as "the
        lesson"."""
        ids = await self._near_duplicates(
            session_factory, config, org, 6, failed_ids={0, 1, 2, 3, 4}
        )
        only_ok = ids[5]
        async with session_scope(session_factory) as session:
            found = await crud.search_traces(session, org, limit=50)
            await crud.holdout_for_results(session, org, found["traces"], "occ-1")
        async with session_scope(session_factory) as session:
            trace_ids = (
                await session.execute(select(func.distinct(HoldoutObservation.trace_id)))
            ).scalars().all()
        assert [str(t) for t in trace_ids] == [only_ok]

    async def test_genuinely_distinct_traces_are_not_merged(self, session_factory, org):
        """The control: unrelated lessons must keep getting independent
        holdout decisions -- clustering must never merge traces that
        share no real content, only coincidental structure."""
        await _traces(session_factory, org, 6)  # the fixture's own unrelated-topics bank
        async with session_scope(session_factory) as session:
            found = await crud.search_traces(session, org, limit=50)
            for i in range(30):
                await crud.holdout_for_results(session, org, found["traces"], f"occ-{i}")
        for i in range(30):
            await _resolve(session_factory, org, f"occ-{i}", True)
        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)
        assert len(report["effects"]) == 6

    async def test_withhold_list_never_names_an_id_outside_the_input(
        self, session_factory, config, org
    ):
        """The wire contract is unchanged: `withhold` is drawn from exactly
        the ids `traces` supplied, even though one shared cluster decision
        produced every verdict in it."""
        ids = await self._near_duplicates(session_factory, config, org, 5)
        async with session_scope(session_factory) as session:
            found = await crud.search_traces(session, org, limit=50)
            holdout = await crud.holdout_for_results(session, org, found["traces"], "occ-1")
        assert set(holdout["withhold"]) <= set(ids)


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

    async def test_starting_pre_registers_what_the_run_will_measure(
        self, session_factory, capsys
    ):
        """Registered at the only moment it means anything. Written later it
        records what the results turned out to be, not what the run set out
        to find."""
        from commontrace import prereg

        async with session_scope(session_factory) as session:
            o = Organization(name="fleet")
            session.add(o)
            await session.flush()
            org_id = o.id

        assert await manage.start_experiment(
            org_id, "0.25", "resolved", "Q1 pilot", session_factory=session_factory
        )
        assert "pre-registered" in capsys.readouterr().out

        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            registered = prereg.Preregistration.from_dict(org.holdout_prereg)

        assert registered.primary_outcome == "resolved"
        assert registered.holdout_rate == 0.25
        assert registered.notes == "Q1 pilot"
        # Bound to THIS randomization: a new salt is a new experiment and
        # must not inherit the last one's credibility.
        assert registered.salt == org.holdout_salt

    async def test_a_registered_run_reports_no_deviations(self, session_factory):
        async with session_scope(session_factory) as session:
            o = Organization(name="fleet")
            session.add(o)
            await session.flush()
            org_id = o.id

        await manage.start_experiment(org_id, "0.25", session_factory=session_factory)
        async with session_scope(session_factory) as session:
            causal = await crud.causal_effects(session, org_id)

        block = causal["preregistration"]
        assert block["registered"] is True
        assert block["clean"] is True, block["deviations"]
        assert block["fingerprint"]

    async def test_restarting_re_registers_against_the_new_salt(self, session_factory):
        """The deviation that matters most: a report checked against a
        registration written for a DIFFERENT randomization is checked against
        nothing."""
        async with session_scope(session_factory) as session:
            o = Organization(name="fleet")
            session.add(o)
            await session.flush()
            org_id = o.id

        await manage.start_experiment(org_id, "0.2", session_factory=session_factory)
        async with session_scope(session_factory) as session:
            first = dict((await session.get(Organization, org_id)).holdout_prereg)
        await manage.start_experiment(org_id, "0.2", session_factory=session_factory)
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            second = dict(org.holdout_prereg)

        assert first["salt"] != second["salt"]
        assert second["salt"] == org.holdout_salt
        assert first["fingerprint"] != second["fingerprint"]

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


class TestTheWorkingSet:
    """Memory that costs its tokens once per session instead of once per
    query -- and the rule that decides what gets in.

    The design worth defending here is that membership is EARNED. Only a
    trace the holdout has established as HELPS is pinned, which is also
    what keeps the method honest: a trace pinned into every session is
    injected on every occasion, so pinning one still under test would
    destroy the control arm still measuring it. A trace is either being
    randomized or it has graduated -- never both.
    """

    async def _established(self, session_factory, org, n=120, helps=True):
        """One trace with a real, established effect in the given direction.

        The occasions are CHOSEN so the realized arm split matches the
        configured rate exactly. Arms are still decided by the production
        hash (`experiment.is_held_out`) -- this only picks which occasion
        ids to run -- and that is what makes the fixture deterministic.

        It has to be. Drawing occasions in order gives a fair binomial
        split, and `integrity.check_arm_balance` reports a split far enough
        from the configured rate as INVALIDATES at ARM_BALANCE_ALPHA=0.001,
        on the stated premise that "assignment is a deterministic hash, so
        this is not sampling noise". For ONE trace over 120 occasions it is
        sampling noise: measured here, a fair draw trips that check 0.067%
        of the time, which across the ~20 experiments this suite builds and
        a 13-job CI matrix is a ~16% chance of one red job per run. It cost
        exactly that once -- `working_set` correctly returned "COMPROMISED,
        no block" and every test built on this helper failed for a reason
        unrelated to what it was testing.
        """
        [trace] = await _traces(session_factory, org, 1)
        async with session_scope(session_factory) as session:
            record = await session.get(Organization, org)
            rate, salt = record.holdout_rate, record.holdout_salt

        want_withheld = n // 2
        withheld: list[str] = []
        injected: list[str] = []
        candidate = 0
        while len(withheld) < want_withheld or len(injected) < n - want_withheld:
            occasion = f"occ-{candidate}"
            candidate += 1
            if experiment.is_held_out(trace, occasion, rate=rate, salt=salt):
                if len(withheld) < want_withheld:
                    withheld.append(occasion)
            elif len(injected) < n - want_withheld:
                injected.append(occasion)

        # 80% success in the arm the effect favours, 30% in the other, so the
        # direction is whatever `helps` asks for and the size is far past any
        # significance threshold.
        for arm_is_injected, occasions in ((False, withheld), (True, injected)):
            favoured = arm_is_injected == helps
            for i, occasion in enumerate(occasions):
                await _assign(session_factory, org, [trace], occasion)
                good = (i % 10 < 8) if favoured else (i % 10 < 3)
                await _resolve(session_factory, org, occasion, good)
        return [trace]

    async def test_a_proven_trace_is_promoted_into_the_block(self, session_factory, org):
        [trace] = await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            ws = await crud.working_set(session, org)
        assert ws["established"] is True
        assert [e["trace_id"] for e in ws["entries"]] == [trace]
        assert trace in ws["block"]
        assert ws["chars_used"] > 0

    async def test_nothing_is_promoted_before_the_experiment_answers(
        self, session_factory, org
    ):
        """An empty block is a statement about evidence, not about the
        corpus -- and it must say so rather than quietly falling back to a
        most-retrieved list that would look identical but carry none."""
        await _traces(session_factory, org, 3)
        async with session_scope(session_factory) as session:
            ws = await crud.working_set(session, org)
        assert ws["established"] is False
        assert ws["entries"] == [] and ws["block"] == ""
        assert "not about the corpus" in ws["reason"] or "evidence" in ws["reason"]

    async def test_a_trace_still_under_test_is_never_pinned(self, session_factory, org):
        """THE invariant. Pinning a trace that is still being randomized
        would inject it on every occasion and destroy its own control arm,
        so an UNDERPOWERED trace must stay out however promising it looks."""
        traces = await _traces(session_factory, org, 1)
        for i in range(8):  # far too few occasions to establish anything
            await _assign(session_factory, org, traces, f"occ-{i}")
            await _resolve(session_factory, org, f"occ-{i}", True)
        async with session_scope(session_factory) as session:
            causal = await crud.causal_effects(session, org)
            ws = await crud.working_set(session, org)
        assert causal["effects"], "expected an effect row to exist but be unestablished"
        assert all(e["verdict"] != "HELPS" for e in causal["effects"])
        assert ws["entries"] == []

    async def test_a_trace_measured_as_hurting_is_never_pinned(self, session_factory, org):
        await self._established(session_factory, org, helps=False)
        async with session_scope(session_factory) as session:
            ws = await crud.working_set(session, org)
        assert ws["entries"] == []

    async def test_a_compromised_experiment_yields_no_block(self, session_factory, org):
        """Same rule as the value figure: a biased selection baked into
        every future session's prompt is the worst place for it."""
        traces = await _traces(session_factory, org, 1)
        for i in range(120):
            result = await _assign(session_factory, org, traces, f"occ-{i}")
            if bool(result["inject"]) or i % 4 == 0:
                await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)
        async with session_scope(session_factory) as session:
            ws = await crud.working_set(session, org)
        assert ws["established"] is False
        assert ws["block"] == ""
        assert "COMPROMISED" in ws["reason"]

    async def test_the_block_respects_its_character_budget(self, session_factory, org):
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            tiny = await crud.working_set(session, org, budget_chars=40)
        assert tiny["chars_used"] <= 40
        assert tiny["budget_chars"] == 40

    async def test_the_gauge_reports_budget_use(self, session_factory, org):
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            ws = await crud.working_set(session, org)
        assert "chars]" in ws["gauge"] and "%" in ws["gauge"]
        assert ws["gauge"] in ws["block"]

    async def test_it_is_stable_across_calls_so_the_prefix_cache_survives(
        self, session_factory, org
    ):
        """The entire cost saving depends on the pasted block not changing
        between sessions for an unchanged corpus -- a block that reshuffled
        would invalidate the prefix cache it exists to preserve."""
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            first = await crud.working_set(session, org)
            second = await crud.working_set(session, org)
        assert first["block"] == second["block"]

    async def test_another_orgs_proven_memory_is_never_visible(
        self, session_factory, org, other_org
    ):
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            theirs = await crud.working_set(session, other_org)
        assert theirs["entries"] == [] and theirs["block"] == ""

    async def test_an_amended_trace_drops_out_of_the_block(self, session_factory, config, org):
        """The same staleness bi-temporal supersession fixed for
        search_traces/commons_visible (hub/models.py:Trace.superseded_at),
        for the ONE surface where it would otherwise never be noticed: this
        block is pinned into a system prompt and never re-fetched mid
        -session. Before this test existed, amending a promoted trace left
        its pre-correction wording pinned here forever -- the effect was
        real, but the text backing it was gone."""
        [trace] = await self._established(session_factory, org)
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            await crud.amend_trace(
                session, org, trace, config, rate_limiter,
                solution_text="the corrected fix", actor="test",
            )
        async with session_scope(session_factory) as session:
            ws = await crud.working_set(session, org)
        assert ws["entries"] == []
        assert ws["established"] is False
        assert "amended or removed" in ws["reason"]

    async def _backdate(self, session_factory, org_id, days):
        """Age this org's whole observation history by `days`.

        The horizon is measured from the last RESOLVED observation behind an
        estimate, so moving `created_at` back is the honest way to reach the
        expiry branch -- the effect, the arms and the verdict all stay exactly
        as the analysis computed them.
        """
        async with session_scope(session_factory) as session:
            await session.execute(
                sa_update(HoldoutObservation)
                .where(HoldoutObservation.org_id == org_id)
                .values(created_at=func.now() - timedelta(days=days))
            )

    async def test_the_fixture_builds_an_uncompromised_experiment(
        self, session_factory, org
    ):
        """Guards the determinism `_established` now buys. Every test in this
        class reads a `working_set` block, and that block is empty whenever
        the integrity audit cannot vouch for the sample -- so a fixture whose
        arm split wanders trips `check_arm_balance` occasionally and fails
        those tests for a reason none of them is about."""
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            causal = await crud.causal_effects(session, org)
        audit = causal["integrity"]
        assert audit["effects_readable"] is True, audit
        balance = next(f for f in audit["findings"] if f["check"] == "arm_balance")
        assert balance["severity"] == "OK", balance
        assert balance["numbers"]["withheld"] == 60
        assert balance["numbers"]["total"] == 120

    async def test_evidence_older_than_the_horizon_is_not_pinned(
        self, session_factory, org
    ):
        """Graduation expires. Pinning a trace stops it being withheld, which
        stops the experiment that measured it -- so an effect nobody has
        observed in a long time is evidence that the lesson worked ONCE, not
        that it still works. Competing systems age memory out on access
        recency because they cannot see the difference; this one can."""
        await self._established(session_factory, org)
        await self._backdate(session_factory, org, days=400)
        async with session_scope(session_factory) as session:
            ws = await crud.working_set(session, org, evidence_horizon_days=180)
        assert ws["entries"] == []
        assert ws["established"] is False
        assert "evidence horizon" in ws["reason"]
        assert "stopped working" in ws["reason"], (
            "the reason must not claim decay it did not measure"
        )

    async def test_evidence_inside_the_horizon_is_still_pinned(
        self, session_factory, org
    ):
        """The other side of the same boundary: an effect measured recently
        keeps its place, so the horizon expires stale evidence rather than
        quietly emptying the block."""
        [trace] = await self._established(session_factory, org)
        await self._backdate(session_factory, org, days=10)
        async with session_scope(session_factory) as session:
            ws = await crud.working_set(session, org, evidence_horizon_days=180)
        assert [e["trace_id"] for e in ws["entries"]] == [trace]
        assert ws["established"] is True

    async def test_each_entry_reports_the_age_of_its_evidence(
        self, session_factory, org
    ):
        """A caller must be able to see how old the evidence behind a pinned
        lesson is -- that is the number every competitor has to approximate
        with an access-recency heuristic."""
        await self._established(session_factory, org)
        await self._backdate(session_factory, org, days=30)
        async with session_scope(session_factory) as session:
            ws = await crud.working_set(session, org)
        [entry] = ws["entries"]
        assert entry["last_measured_at"], "an effect must carry the date it was last measured"
        assert 29 <= entry["evidence_age_days"] <= 31

    async def test_the_age_is_kept_out_of_the_pinned_block(self, session_factory, org):
        """The block's text must not carry anything that changes daily: a
        block whose text moves invalidates the prefix cache on every session,
        which is the whole saving this function exists to produce."""
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            fresh = await crud.working_set(session, org)
        await self._backdate(session_factory, org, days=30)
        async with session_scope(session_factory) as session:
            aged = await crud.working_set(session, org)
        assert aged["block"] == fresh["block"], "block text must not depend on evidence age"
        assert aged["entries"][0]["evidence_age_days"] != fresh["entries"][0]["evidence_age_days"]

    async def test_causal_effects_dates_every_estimate(self, session_factory, org):
        """The audit surface carries the date too -- `working_set` reads it
        from there rather than re-querying, so the two can never disagree
        about when a trace was last measured."""
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            causal = await crud.causal_effects(session, org)
        assert causal["effects"]
        assert all(e["last_measured_at"] for e in causal["effects"])

    async def test_a_purged_trace_drops_out_of_the_block(self, session_factory, org):
        """Same fix, other cause: hub/manage.py:purge_trace removes a row
        entirely rather than superseding it. Before this test existed the
        block pinned a '(deleted trace)' placeholder title with an empty
        solution body -- worse than stale, since there was nothing to read."""
        [trace] = await self._established(session_factory, org)
        await manage.purge_trace(trace, session_factory=session_factory)
        async with session_scope(session_factory) as session:
            ws = await crud.working_set(session, org)
        assert ws["entries"] == []
        assert ws["established"] is False


class TestAPinnedTraceIsNotItsOwnControl:
    """`working_set` puts a trace in the system prompt for a whole session.
    Nothing stopped the experiment from later drawing that same trace into
    the WITHHELD arm for a search result -- and a withheld occasion whose
    trace is still sitting in the prompt is a treated occasion recorded as a
    control. `holdout_assign`'s own note describes the consequence exactly:
    it "does not fail loudly -- it biases the measured effect toward zero".

    `working_set`'s docstring already claimed a trace is "either being
    randomized or it has graduated, never both". Only the caller knows what
    it actually pasted, so `pinned` is how it says so.
    """

    async def test_a_pinned_trace_is_never_withheld(self, session_factory, org):
        traces = await _traces(session_factory, org, 3)
        withheld_somewhere = False
        for i in range(40):
            result = await _assign(session_factory, org, traces, f"occ-{i}")
            if traces[0] in result["withhold"]:
                withheld_somewhere = True
                break
        assert withheld_somewhere, "expected this trace to land in the control arm unpinned"

        pinned_result = await _assign_pinned(
            session_factory, org, traces, "pinned-occ", pinned=[traces[0]]
        )
        assert traces[0] not in pinned_result["withhold"]
        assert traces[0] in pinned_result["inject"]
        assert pinned_result["pinned"] == [traces[0]]

    async def test_a_pinned_trace_records_no_observation(self, session_factory, org):
        """The load-bearing assertion. Reporting it as `inject` would be no
        better than withholding it if the row still landed: the occasion
        would join the treated arm and inflate its own effect. It must
        contribute to NEITHER arm."""
        traces = await _traces(session_factory, org, 2)
        await _assign_pinned(
            session_factory, org, traces, "occ-pinned", pinned=[traces[0]]
        )
        async with session_scope(session_factory) as session:
            rows = (
                await session.execute(
                    select(HoldoutObservation.trace_id).where(
                        HoldoutObservation.org_id == org,
                        HoldoutObservation.occasion_id == "occ-pinned",
                    )
                )
            ).scalars().all()
        assert traces[0] not in rows, "a pinned trace must contribute to neither arm"
        assert traces[1] in rows, "unpinned traces must still be measured normally"

    async def test_omitting_pinned_changes_nothing(self, session_factory, org):
        """Strictly additive: a client that never passes `pinned` behaves
        exactly as it did before the parameter existed."""
        traces = await _traces(session_factory, org, 2)
        plain = await _assign(session_factory, org, traces, "occ-plain")
        assert plain["pinned"] == []
        assert sorted(plain["inject"] + plain["withhold"]) == sorted(traces)

    async def test_a_pinned_trace_cannot_return_as_a_cluster_representative(
        self, session_factory, config, org
    ):
        """`holdout_for_results` clusters near-duplicates and randomizes the
        whole cluster under one representative. Dropping pinned ids only
        AFTER assignment would miss this: a pinned trace elected as the
        representative would hand its arm to every duplicate. They are
        dropped before clustering for exactly this reason."""
        rate_limiter = make_rate_limiter(config)
        made = []
        async with session_scope(session_factory) as session:
            for i in range(3):
                t = await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="connection pool exhaustion under load",
                    context_text="the pool runs out of connections under concurrent load",
                    solution_text=f"raise the pool ceiling and add backpressure ({i})",
                    tags=["db"], agent_type="code", actor="test",
                )
                made.append(t["id"])

        traces = [{"id": t} for t in made]
        async with session_scope(session_factory) as session:
            holdout = await crud.holdout_for_results(
                session, org, traces, "occ-cluster", pinned=[made[0]]
            )
        assert made[0] not in holdout.get("withhold", [])

        async with session_scope(session_factory) as session:
            rows = (
                await session.execute(
                    select(HoldoutObservation.trace_id).where(
                        HoldoutObservation.org_id == org,
                        HoldoutObservation.occasion_id == "occ-cluster",
                    )
                )
            ).scalars().all()
        assert made[0] not in rows


class TestTieredValuationAndTheAuditLedger:
    """Pricing the measured occasions against a contractual rate card, and
    handing the customer a chain they can recompute themselves."""

    async def _established(self, session_factory, org, n=120):
        return await TestTheWorkingSet()._established(session_factory, org, n=n)

    _TIERS = [
        {"name": "L1 informational", "share": 0.55, "cost_per_occasion": 8.0},
        {"name": "L2 transactional", "share": 0.30, "cost_per_occasion": 35.0},
        {"name": "L3 technical", "share": 0.13, "cost_per_occasion": 120.0},
        {"name": "critical escalation", "share": 0.02, "cost_per_occasion": 350.0},
    ]

    async def test_a_rate_card_prices_the_run_and_is_echoed_as_an_input(
        self, session_factory, org
    ):
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            worth = await crud.value_delivered(session, org, rate_tiers=self._TIERS)
        blended = sum(t["share"] * t["cost_per_occasion"] for t in self._TIERS)
        assert worth["rate_applied"] == pytest.approx(blended)
        assert worth["money"] == pytest.approx(worth["occasions_improved"] * blended)
        assert [t["name"] for t in worth["rate_tiers"]] == [
            t["name"] for t in self._TIERS
        ], "the tiers must come back as the inputs they are"

    async def test_the_ledger_verifies_and_covers_every_counted_memory(
        self, session_factory, org
    ):
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            worth = await crud.value_delivered(session, org, rate_tiers=self._TIERS)
        assert worth["ledger"], "a priced, readable run must carry a ledger"
        entries = [
            value.LedgerEntry(
                index=e["index"], slug=e["trace_id"], verdict=e["verdict"],
                occasions_improved=e["occasions_improved"], rate=e["rate"],
                money=e["money"], previous_hash=e["previous_hash"],
                entry_hash=e["entry_hash"],
            )
            for e in worth["ledger"]
        ]
        assert value.verify_ledger(entries) is None
        counted = [m for m in worth["memories"] if m["counted"]]
        assert len(entries) == len(counted)

    async def test_co_injected_traces_produce_no_total_but_still_a_policy_figure(
        self, session_factory, org
    ):
        """The Hub's NORMAL case, not an edge one: holdout_assign takes a
        LIST of traces for one occasion, so two traces routinely share
        occasions -- and adding their contributions would attribute one
        improved occasion twice, then price it twice.

        Refusing the sum is correct and, alone, useless: it would be the
        answer for almost every real fleet. The policy-level comparison
        (occasions that got any memory against occasions that got none)
        counts each occasion exactly once, so it survives, and is what the
        customer is told instead."""
        [first] = await self._established(session_factory, org, n=120)
        # A second trace assigned on the SAME occasions the first ran on.
        [second] = await _traces(session_factory, org, 1)
        for i in range(120):
            occasion = f"occ-{i}"
            await _assign(session_factory, org, [second], occasion)

        async with session_scope(session_factory) as session:
            causal = await crud.causal_effects(session, org)
            worth = await crud.value_delivered(session, org, value_per_occasion=25.0)

        shared = {frozenset(p) for p in causal["co_injection"]["pairs"]}
        assert frozenset((first, second)) in shared, (
            "the two traces ran on the same occasions, which is what this asserts "
            "the Hub notices"
        )
        # Whether the SUM is offered depends on which traces ended up counted;
        # what must never happen is a priced total over co-injected traces.
        if not worth["aggregate_readable"]:
            assert worth["money"] is None
            assert worth["ledger"] == []
            assert "overlapping occasions" in worth["aggregate_reason"]
        # The valid aggregate is reported either way, on unique occasions.
        policy = worth["policy_effect"]
        assert policy is not None
        assert policy["n_treated"] + policy["n_control"] <= 120, (
            "one occasion must count once, however many traces it received"
        )

    async def test_no_signing_key_leaves_the_ledger_unsigned_but_explicit(
        self, session_factory, org
    ):
        """verify_ledger alone proves the chain is internally consistent, not
        who issued it -- its genesis and algorithm are both public, so a
        party with write access to storage could fabricate an entire
        replacement chain that verifies just as cleanly. Without a signing
        key configured, value_delivered must say so rather than silently
        returning a ledger that looks more authenticated than it is."""
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            worth = await crud.value_delivered(session, org, rate_tiers=self._TIERS)
        assert worth["signature"] is None
        assert worth["signature_algorithm"] is None
        assert "not configured" in worth["signature_reason"]
        assert worth["issued_at"]  # still timestamped even when unsigned

    async def test_a_signing_key_produces_a_verifiable_signature(
        self, session_factory, org
    ):
        await self._established(session_factory, org)
        key = "test-issuer-signing-key"
        async with session_scope(session_factory) as session:
            worth = await crud.value_delivered(
                session, org, rate_tiers=self._TIERS, signing_key=key,
            )
        assert worth["signature"]
        assert worth["signature_algorithm"] == "HMAC-SHA256"
        assert worth["signature_reason"] == ""
        entries = [
            value.LedgerEntry(
                index=e["index"], slug=e["trace_id"], verdict=e["verdict"],
                occasions_improved=e["occasions_improved"], rate=e["rate"],
                money=e["money"], previous_hash=e["previous_hash"],
                entry_hash=e["entry_hash"],
            )
            for e in worth["ledger"]
        ]
        anchors = {
            "evidence_digest": worth["evidence_digest"],
            "prereg_fingerprint": worth["preregistration"]["fingerprint"],
        }
        assert value.verify_ledger_signature(
            entries, worth["signature"], key.encode("utf-8"),
            org_id=org, issued_at=worth["issued_at"], **anchors,
        )
        # The wrong key -- or the right key against a signature minted for a
        # different org -- must not verify.
        assert not value.verify_ledger_signature(
            entries, worth["signature"], b"wrong-key",
            org_id=org, issued_at=worth["issued_at"], **anchors,
        )
        # And the anchoring itself: the signature covers WHICH assignment
        # rows the invoice was computed from, so it cannot be presented
        # alongside a different export.
        assert not value.verify_ledger_signature(
            entries, worth["signature"], key.encode("utf-8"),
            org_id=org, issued_at=worth["issued_at"],
            evidence_digest="a-different-data-set",
            prereg_fingerprint=anchors["prereg_fingerprint"],
        )

    async def test_the_report_carries_the_evidence_it_was_computed_from(
        self, session_factory, org
    ):
        """Every number this product bills on is computed by this product.
        The digest is what lets a customer check that the assignment export
        they re-ran the arithmetic from is the one the invoice came from."""
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            worth = await crud.value_delivered(session, org, rate_tiers=self._TIERS)
            exported = await crud.holdout_assignments(session, org)

        from commontrace import raw_export

        assert worth["evidence_digest"] == raw_export.digest_of(exported)
        result = raw_export.export(exported)
        assert raw_export.verify(result.csv_text, worth["evidence_digest"])

    async def test_an_unregistered_experiment_says_so_rather_than_passing(
        self, session_factory, org
    ):
        """Silence would read as approval. These fixtures start an experiment
        directly rather than through `hub.manage start-experiment`, so there
        is no registration -- and the absence is itself the finding."""
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            causal = await crud.causal_effects(session, org)
        prereg_block = causal["preregistration"]
        assert prereg_block["registered"] is False
        assert "not pre-registered" in prereg_block["note"]

    async def test_no_rate_means_a_count_and_no_ledger(self, session_factory, org):
        """Unchanged behaviour for every existing caller: ask for no price and
        you get the measured quantity, with nothing implying an invoice."""
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            worth = await crud.value_delivered(session, org)
        assert worth["occasions_improved"] > 0
        assert worth["money"] is None
        assert worth["ledger"] == []

    async def test_a_malformed_rate_card_is_refused_rather_than_ignored(
        self, session_factory, org
    ):
        """Shares that do not cover every occasion price a volume nobody
        measured. Falling back to some other number would be worse than the
        error: the customer would be invoiced against an assumption they
        thought they had replaced."""
        await self._established(session_factory, org)
        with pytest.raises(ValueError, match="sum to"):
            async with session_scope(session_factory) as session:
                await crud.value_delivered(
                    session, org,
                    rate_tiers=[{"name": "half", "share": 0.5, "cost_per_occasion": 10.0}],
                )


class TestTheOperatorCLIReachesTheSameNumbers:
    """`hub.manage value` -- crud.value_delivered from the operator CLI.

    Before this command existed, the same numbers were reachable from a
    customer's own browser session (hub/console.py) and from an
    authenticated agent (hub/server.py's MCP tool), but an operator
    investigating one account had no CLI path to them at all. Reuses
    TestWhatTheMemoryWasWorth's own experiment-seeding helper rather than
    duplicating it.
    """

    async def _running_experiment(self, session_factory, org, n=120, helps=True):
        return await TestWhatTheMemoryWasWorth()._running_experiment(
            session_factory, org, n=n, helps=helps
        )

    async def test_an_unknown_org_reports_an_error(self, session_factory, capsys):
        assert not await manage.value("00000000-0000-0000-0000-000000000000",
                                       session_factory=session_factory)
        assert "no such organization" in capsys.readouterr().err

    async def test_a_non_numeric_rate_reports_an_error_without_touching_the_db(
        self, session_factory, org, capsys
    ):
        assert not await manage.value(org, "free", session_factory=session_factory)
        assert "must be a number" in capsys.readouterr().err

    async def test_no_activity_yet_reports_zero_rather_than_erroring(
        self, session_factory, org, capsys
    ):
        assert await manage.value(org, session_factory=session_factory)
        out = capsys.readouterr().out
        assert "occasions improved: 0.0" in out

    async def test_with_no_rate_it_prints_a_count_but_no_money(self, session_factory, org, capsys):
        await self._running_experiment(session_factory, org)
        assert await manage.value(org, session_factory=session_factory)
        out = capsys.readouterr().out
        assert "occasions improved" in out
        assert "pass a value-per-occasion rate" in out
        # No currency is ever printed unless a rate was actually supplied.
        assert "share" not in out

    async def test_with_a_rate_it_prints_the_capture_share(self, session_factory, org, capsys):
        await self._running_experiment(session_factory, org)
        async with session_scope(session_factory) as session:
            expected = await crud.value_delivered(session, org, value_per_occasion=25.0)

        assert await manage.value(org, "25.0", session_factory=session_factory)
        out = capsys.readouterr().out
        assert f"{expected['money']:,.2f}" in out
        billable = expected["money"] * plans.VALUE_CAPTURE_SHARE
        assert f"{billable:,.2f}" in out

    async def test_a_compromised_experiment_prints_the_reason_not_a_figure(
        self, session_factory, org, capsys
    ):
        traces = await _traces(session_factory, org, 1)
        for i in range(120):
            result = await _assign(session_factory, org, traces, f"occ-{i}")
            if bool(result["inject"]) or i % 4 == 0:
                await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)

        assert await manage.value(org, "25.0", session_factory=session_factory)
        out = capsys.readouterr().out
        assert "not readable" in out
        assert "COMPROMISED" in out
