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
    "duplicate message delivery from at least once queue semantics",
    "silent truncation of oversized payload in log ingestion",
    "misconfigured retry budget starving a downstream dependency",
    "index bloat from unvacuumed table slowing point lookups",
    "leaked file descriptors exhausting the process open file limit",
    "inconsistent read replica lag returning stale account balances",
    "broken pagination cursor skipping rows under concurrent inserts",
    "misapplied feature flag rollout enabling code path for wrong cohort",
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
        [trace] = await _traces(session_factory, org, 1)
        arms = set()
        for i in range(40):
            result = await _assign(session_factory, org, [trace], f"occ-{i}")
            arms.add(bool(result["inject"]))
        assert arms == {True, False}

    async def test_the_same_call_twice_returns_the_same_arms(self, session_factory, org):
        traces = await _traces(session_factory, org, 6)
        first = await _assign(session_factory, org, traces, "occ-1")
        second = await _assign(session_factory, org, traces, "occ-1")
        assert first["inject"] == second["inject"]
        assert first["withhold"] == second["withhold"]

    async def test_a_retry_records_no_duplicate_rows(self, session_factory, org):
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
        a, b = await _traces(session_factory, org, 2)
        together = 0
        for i in range(60):
            r = await _assign(session_factory, org, [a, b], f"occ-{i}")
            if len(r["inject"]) in (0, 2):
                together += 1
        assert 10 < together < 50, together

    async def test_another_orgs_trace_is_silently_absent(self, session_factory, org, other_org):
        mine = await _traces(session_factory, org, 1)
        theirs = await _traces(session_factory, other_org, 1)
        result = await _assign(session_factory, org, mine + theirs, "occ-1")
        assert set(result["inject"]) | set(result["withhold"]) == set(mine)

    async def test_no_experiment_running_is_a_clean_error(self, session_factory):
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
    async def test_no_occasion_id_leaves_search_completely_unchanged(
        self, session_factory, org
    ):
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
        traces = await _traces(session_factory, org, 20)
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
    async def _near_duplicates(self, session_factory, config, org_id, n, *, failed_ids=()):
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
        await self._near_duplicates(session_factory, config, org, 8)
        async with session_scope(session_factory) as session:
            found = await crud.search_traces(session, org, limit=50)
            holdout = await crud.holdout_for_results(session, org, found["traces"], "occ-1")
        withheld = set(holdout["withhold"])
        all_ids = {t["id"] for t in found["traces"]}
        assert withheld == all_ids or withheld == set()

    async def test_observations_accumulate_on_one_trace_id(self, session_factory, config, org):
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
        canonical = (await self._near_duplicates(session_factory, config, org, 1))[0]
        for i in range(12):
            async with session_scope(session_factory) as session:
                found = await crud.search_traces(session, org, query="connection pool exhausted", limit=5)
                await crud.holdout_for_results(session, org, found["traces"], f"occ-{i}")
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
        await _traces(session_factory, org, 6)
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
        ids = await self._near_duplicates(session_factory, config, org, 5)
        async with session_scope(session_factory) as session:
            found = await crud.search_traces(session, org, limit=50)
            holdout = await crud.holdout_for_results(session, org, found["traces"], "occ-1")
        assert set(holdout["withhold"]) <= set(ids)


class TestRecordingOutcomes:
    async def test_an_outcome_resolves_both_arms_at_once(self, session_factory, org):
        traces = await _traces(session_factory, org, 8)
        await _assign(session_factory, org, traces, "occ-1")
        result = await _resolve(session_factory, org, "occ-1", True)
        assert result["observations_resolved"] == 8

    async def test_a_second_report_cannot_flip_a_counted_result(self, session_factory, org):
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
    async def test_the_report_carries_a_validity_verdict(self, session_factory, org):
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
        traces = await _traces(session_factory, org, 1)
        for i in range(30):
            await _assign(session_factory, org, traces, f"occ-{i}")
            await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)

        checks = {f["check"] for f in report["integrity"]["findings"]}
        assert {"differential_attrition", "arm_balance", "consistent_arms"} <= checks

    async def test_a_reporting_gap_in_one_arm_is_caught_and_named(self, session_factory, org):
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
        assert integrity_report["n_assignments"] > integrity_report["n_resolved"]

    async def test_a_compromised_report_says_so_in_the_note_an_agent_reads(
        self, session_factory, org
    ):
        traces = await _traces(session_factory, org, 1)
        for i in range(120):
            result = await _assign(session_factory, org, traces, f"occ-{i}")
            if bool(result["inject"]) or i % 4 == 0:
                await _resolve(session_factory, org, f"occ-{i}", i % 2 == 0)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)

        assert "READ `integrity` FIRST" in report["note"]

    async def test_it_projects_when_each_trace_becomes_answerable(self, session_factory, org):
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
        traces = await _traces(session_factory, org, 1)
        await _assign(session_factory, org, traces, "occ-1")
        await _resolve(session_factory, org, "occ-1", True)

        async with session_scope(session_factory) as session:
            report = await crud.causal_effects(session, org)

        assert "withhold" in report["integrity"]["not_checkable"]

    async def test_two_experiments_still_never_pool(self, session_factory, org):
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
    async def _running_experiment(self, session_factory, org, n=120, helps=True):
        topic = _UNRELATED_TOPICS[0]
        async with session_scope(session_factory) as session:
            t = Trace(
                id="22222222-2222-4222-8222-222222222222",
                org_id=org, title="lesson 0",
                context_text=topic,
                solution_text=f"resolved by addressing {topic}",
                tags=[], agent_type="code",
            )
            session.add(t)
            await session.flush()
            traces = [t.id]
        for i in range(n):
            result = await _assign(session_factory, org, traces, f"occ-{i}")
            injected = bool(result["inject"])
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
        assert counted["money"] is None
        assert counted["value_per_occasion"] is None
        assert priced["money"] == pytest.approx(
            priced["occasions_improved"] * 25.0, rel=1e-6)
        assert priced["money_range"] is not None

    async def test_a_compromised_experiment_yields_no_figure(self, session_factory, org):
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
    async def _established(self, session_factory, org, n=120, helps=True):
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
        await _traces(session_factory, org, 3)
        async with session_scope(session_factory) as session:
            ws = await crud.working_set(session, org)
        assert ws["established"] is False
        assert ws["entries"] == [] and ws["block"] == ""
        assert "not about the corpus" in ws["reason"] or "evidence" in ws["reason"]

    async def test_a_trace_still_under_test_is_never_pinned(self, session_factory, org):
        traces = await _traces(session_factory, org, 1)
        for i in range(8):
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
        async with session_scope(session_factory) as session:
            await session.execute(
                sa_update(HoldoutObservation)
                .where(HoldoutObservation.org_id == org_id)
                .values(created_at=func.now() - timedelta(days=days))
            )

    async def test_the_fixture_builds_an_uncompromised_experiment(
        self, session_factory, org
    ):
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
        [trace] = await self._established(session_factory, org)
        await self._backdate(session_factory, org, days=10)
        async with session_scope(session_factory) as session:
            ws = await crud.working_set(session, org, evidence_horizon_days=180)
        assert [e["trace_id"] for e in ws["entries"]] == [trace]
        assert ws["established"] is True

    async def test_each_entry_reports_the_age_of_its_evidence(
        self, session_factory, org
    ):
        await self._established(session_factory, org)
        await self._backdate(session_factory, org, days=30)
        async with session_scope(session_factory) as session:
            ws = await crud.working_set(session, org)
        [entry] = ws["entries"]
        assert entry["last_measured_at"], "an effect must carry the date it was last measured"
        assert 29 <= entry["evidence_age_days"] <= 31

    async def test_the_age_is_kept_out_of_the_pinned_block(self, session_factory, org):
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            fresh = await crud.working_set(session, org)
        await self._backdate(session_factory, org, days=30)
        async with session_scope(session_factory) as session:
            aged = await crud.working_set(session, org)
        assert aged["block"] == fresh["block"], "block text must not depend on evidence age"
        assert aged["entries"][0]["evidence_age_days"] != fresh["entries"][0]["evidence_age_days"]

    async def test_causal_effects_dates_every_estimate(self, session_factory, org):
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            causal = await crud.causal_effects(session, org)
        assert causal["effects"]
        assert all(e["last_measured_at"] for e in causal["effects"])

    async def test_a_purged_trace_drops_out_of_the_block(self, session_factory, org):
        [trace] = await self._established(session_factory, org)
        await manage.purge_trace(trace, session_factory=session_factory)
        async with session_scope(session_factory) as session:
            ws = await crud.working_set(session, org)
        assert ws["entries"] == []
        assert ws["established"] is False


class TestAPinnedTraceIsNotItsOwnControl:
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
        traces = await _traces(session_factory, org, 2)
        plain = await _assign(session_factory, org, traces, "occ-plain")
        assert plain["pinned"] == []
        assert sorted(plain["inject"] + plain["withhold"]) == sorted(traces)

    async def test_a_pinned_trace_cannot_return_as_a_cluster_representative(
        self, session_factory, config, org
    ):
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
        [first] = await self._established(session_factory, org, n=120)
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
        if not worth["aggregate_readable"]:
            assert worth["money"] is None
            assert worth["ledger"] == []
            assert "overlapping occasions" in worth["aggregate_reason"]
        policy = worth["policy_effect"]
        assert policy is not None
        assert policy["n_treated"] + policy["n_control"] <= 120, (
            "one occasion must count once, however many traces it received"
        )

    async def test_no_signing_key_leaves_the_ledger_unsigned_but_explicit(
        self, session_factory, org
    ):
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            worth = await crud.value_delivered(session, org, rate_tiers=self._TIERS)
        assert worth["signature"] is None
        assert worth["signature_algorithm"] is None
        assert "not configured" in worth["signature_reason"]
        assert worth["issued_at"]

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
        assert not value.verify_ledger_signature(
            entries, worth["signature"], b"wrong-key",
            org_id=org, issued_at=worth["issued_at"], **anchors,
        )
        assert not value.verify_ledger_signature(
            entries, worth["signature"], key.encode("utf-8"),
            org_id=org, issued_at=worth["issued_at"],
            evidence_digest="a-different-data-set",
            prereg_fingerprint=anchors["prereg_fingerprint"],
        )

    async def test_the_report_carries_the_evidence_it_was_computed_from(
        self, session_factory, org
    ):
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
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            causal = await crud.causal_effects(session, org)
        prereg_block = causal["preregistration"]
        assert prereg_block["registered"] is False
        assert "not pre-registered" in prereg_block["note"]

    async def test_no_rate_means_a_count_and_no_ledger(self, session_factory, org):
        await self._established(session_factory, org)
        async with session_scope(session_factory) as session:
            worth = await crud.value_delivered(session, org)
        assert worth["occasions_improved"] > 0
        assert worth["money"] is None
        assert worth["ledger"] == []

    async def test_a_malformed_rate_card_is_refused_rather_than_ignored(
        self, session_factory, org
    ):
        await self._established(session_factory, org)
        with pytest.raises(ValueError, match="sum to"):
            async with session_scope(session_factory) as session:
                await crud.value_delivered(
                    session, org,
                    rate_tiers=[{"name": "half", "share": 0.5, "cost_per_occasion": 10.0}],
                )


class TestTheOperatorCLIReachesTheSameNumbers:
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


class TestEvidenceDecayReachesTheInvoice:
    async def _established(self, session_factory, org, n=120, helps=True):
        return await TestTheWorkingSet()._established(
            session_factory, org, n=n, helps=helps)

    async def _backdate(self, session_factory, org, days):
        await TestTheWorkingSet()._backdate(session_factory, org, days)

    async def test_fresh_evidence_is_billed(self, session_factory, org):
        await self._established(session_factory, org)
        await self._backdate(session_factory, org, days=10)
        async with session_scope(session_factory) as session:
            worth = await crud.value_delivered(session, org, value_per_occasion=25.0)
        assert worth["n_counted"] == 1
        assert worth["occasions_improved"] > 0

    async def test_evidence_past_the_horizon_is_not_billed(self, session_factory, org):
        await self._established(session_factory, org)
        await self._backdate(session_factory, org, days=400)
        async with session_scope(session_factory) as session:
            worth = await crud.value_delivered(session, org, value_per_occasion=25.0)
        assert worth["n_counted"] == 0
        assert worth["occasions_improved"] == 0

    async def test_the_withheld_memory_says_why(self, session_factory, org):
        await self._established(session_factory, org)
        await self._backdate(session_factory, org, days=400)
        async with session_scope(session_factory) as session:
            worth = await crud.value_delivered(session, org, value_per_occasion=25.0)
        [memory] = worth["memories"]
        assert not memory["counted"]
        assert "evidence" in memory["why_not"]
        assert "Re-run the holdout" in memory["why_not"]

    async def test_a_stale_HARM_is_still_counted(self, session_factory, org):
        await self._established(session_factory, org, helps=False)
        await self._backdate(session_factory, org, days=400)
        async with session_scope(session_factory) as session:
            worth = await crud.value_delivered(session, org, value_per_occasion=25.0)
        [memory] = worth["memories"]
        assert memory["verdict"] == experiment.VERDICT_HURTS
        assert memory["counted"], "a stale harm must not vanish from the invoice"
        assert worth["occasions_improved"] < 0

    async def test_the_horizon_can_be_turned_off_for_a_historical_figure(
        self, session_factory, org
    ):
        await self._established(session_factory, org)
        await self._backdate(session_factory, org, days=400)
        async with session_scope(session_factory) as session:
            worth = await crud.value_delivered(
                session, org, value_per_occasion=25.0, evidence_horizon_days=None)
        assert worth["n_counted"] == 1

    async def test_the_hub_and_the_ledger_share_one_horizon(self):
        from commontrace import decay

        assert crud.DEFAULT_EVIDENCE_HORIZON_DAYS == decay.DEFAULT_HORIZON_DAYS
