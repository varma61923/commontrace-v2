from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from hub import commons, crud, plans
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace, Vote

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="concurrency-audit-org")
        session.add(o)
        await session.flush()
        return o.id


async def _contribute(session_factory, config, org_id, title, context, solution, tags=None, actor="test"):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        return await crud.contribute_trace(
            session, org_id, config, rate_limiter,
            title=title, context_text=context, solution_text=solution,
            tags=tags or [], agent_type="code", actor=actor,
        )


async def _seed_kb(session_factory, operator_org_id, title):
    async with session_scope(session_factory) as session:
        trace = Trace(
            org_id=operator_org_id,
            title=title, context_text="c", solution_text="s",
            tags=[], agent_type="code",
            shared_with_commons=True,
            shared_at=datetime.now(timezone.utc),
            shared_rationale="test fixture",
            commons_signature=commons.signature_for(title, "c", []),
            commons_source="seed",
        )
        session.add(trace)
        await session.flush()
        return trace.id


async def _make_orgs(session_factory, n, name_prefix):
    async with session_scope(session_factory) as session:
        orgs = [Organization(name=f"{name_prefix}-{i}") for i in range(n)]
        session.add_all(orgs)
        await session.flush()
        return [o.id for o in orgs]


class TestVoteTraceRace:
    async def _fire_concurrent_votes(self, session_factory, org_id, trace_id, n=20):
        async def _vote(i):
            vote_type = "up" if i % 2 == 0 else "down"
            async with session_scope(session_factory) as session:
                return await crud.vote_trace(
                    session, org_id, trace_id, vote_type, actor=f"concurrent-{i}"
                )

        return await asyncio.gather(*[_vote(i) for i in range(n)], return_exceptions=True)

    async def test_20_concurrent_votes_same_trace_same_org(self, session_factory, config, org):
        trace = await _contribute(session_factory, config, org, "vote race", "c", "s")

        for run in range(3):
            results = await self._fire_concurrent_votes(session_factory, org, trace["id"], n=20)

            exceptions = [r for r in results if isinstance(r, BaseException)]
            succeeded = [r for r in results if not isinstance(r, BaseException)]

            async with session_scope(session_factory) as session:
                votes = (
                    await session.execute(select(Vote).where(Vote.trace_id == trace["id"]))
                ).scalars().all()
                t = await session.get(Trace, trace["id"])

            print(
                f"[vote race] run={run} n_succeeded={len(succeeded)}/20 "
                f"n_exceptions={len(exceptions)} "
                f"n_vote_rows={len(votes)} "
                f"final_vote_type={votes[0].vote_type if votes else None} "
                f"trust={t.trust}"
            )

            assert not exceptions, f"unexpected exceptions under concurrent voting: {exceptions}"
            assert len(succeeded) == 20
            assert len(votes) == 1, f"expected exactly 1 Vote row, found {len(votes)}"
            expected_trust = 1.0 if votes[0].vote_type == "up" else 0.0
            assert t.trust == pytest.approx(expected_trust)

    async def test_20_concurrent_votes_from_20_distinct_orgs_all_count(
        self, session_factory, config, org, establish_orgs
    ):
        trace_id = await _seed_kb(session_factory, org, "vote race across orgs")
        voter_orgs = await _make_orgs(session_factory, 20, "voter")
        await establish_orgs(voter_orgs)

        async def _vote(voter_org_id):
            async with session_scope(session_factory) as session:
                return await crud.vote_trace(session, voter_org_id, trace_id, "up", actor="concurrent")

        for run in range(3):
            results = await asyncio.gather(*[_vote(o) for o in voter_orgs], return_exceptions=True)
            exceptions = [r for r in results if isinstance(r, BaseException)]

            async with session_scope(session_factory) as session:
                votes = (await session.execute(select(Vote).where(Vote.trace_id == trace_id))).scalars().all()
                t = await session.get(Trace, trace_id)

            print(
                f"[cross-org vote race] run={run} n_exceptions={len(exceptions)} "
                f"n_vote_rows={len(votes)} trust={t.trust} commons_votes={t.commons_votes}"
            )

            assert not exceptions, f"unexpected exceptions under concurrent cross-org voting: {exceptions}"
            assert len(votes) == 20, f"expected 20 Vote rows (one per org), found {len(votes)}"
            assert t.commons_votes == 20, f"commons_votes lost a concurrent voter's contribution: {t.commons_votes}"
            assert t.trust == pytest.approx(1.0), "all 20 votes were 'up'; trust must reflect all of them"

    async def test_cross_org_cannot_vote_on_a_trace_it_does_not_own(self, session_factory, config, org):
        trace = await _contribute(session_factory, config, org, "t", "c", "s")
        async with session_scope(session_factory) as session:
            other_org = Organization(name="other-org")
            session.add(other_org)
            await session.flush()
            other_org_id = other_org.id

        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, other_org_id, trace["id"], "up")
        assert result is None


class TestContributeTraceIdempotency:
    async def test_identical_retry_with_same_key_returns_the_original_not_a_duplicate(
        self, session_factory, config, org
    ):
        args = dict(
            title="idempotency probe",
            context_text="identical context",
            solution_text="identical solution",
            tags=["dup"],
            agent_type="code",
            actor="client-retry-test",
            idempotency_key="client-key-1",
        )
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            r1 = await crud.contribute_trace(session, org, config, rate_limiter, **args)
        async with session_scope(session_factory) as session:
            r2 = await crud.contribute_trace(session, org, config, rate_limiter, **args)

        print(f"[idempotency] first id={r1['id']} second id={r2['id']}")

        async with session_scope(session_factory) as session:
            rows = (
                await session.execute(select(Trace).where(Trace.org_id == org))
            ).scalars().all()

        assert r1["id"] == r2["id"], "retry with the same key must return the original trace, not a new one"
        assert len(rows) == 1, f"expected exactly 1 row after 2 identical-key calls, found {len(rows)}"

    async def test_omitted_key_is_unaffected_and_still_duplicates(self, session_factory, config, org):
        args = dict(
            title="no key", context_text="c", solution_text="s", tags=[], agent_type="code",
        )
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            r1 = await crud.contribute_trace(session, org, config, rate_limiter, **args)
        async with session_scope(session_factory) as session:
            r2 = await crud.contribute_trace(session, org, config, rate_limiter, **args)
        assert r1["id"] != r2["id"]

    async def test_same_key_different_payload_raises_conflict_not_stale_data(
        self, session_factory, config, org
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="original", context_text="c", solution_text="s", tags=[], agent_type="code",
                idempotency_key="reused-key",
            )
        with pytest.raises(crud.IdempotencyKeyConflict):
            async with session_scope(session_factory) as session:
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="a genuinely different request", context_text="c2", solution_text="s2",
                    tags=[], agent_type="code", idempotency_key="reused-key",
                )

    async def test_20_concurrent_identical_retries_create_exactly_one_trace(
        self, session_factory, config, org
    ):
        rate_limiter = make_rate_limiter(config)
        args = dict(
            title="concurrent idempotency probe", context_text="c", solution_text="s",
            tags=[], agent_type="code", idempotency_key="concurrent-key",
        )

        async def _call():
            async with session_scope(session_factory) as session:
                return await crud.contribute_trace(session, org, config, rate_limiter, **args)

        results = await asyncio.gather(*[_call() for _ in range(20)], return_exceptions=True)
        exceptions = [r for r in results if isinstance(r, BaseException)]
        ids = {r["id"] for r in results if not isinstance(r, BaseException)}

        async with session_scope(session_factory) as session:
            rows = (
                await session.execute(
                    select(Trace).where(Trace.org_id == org, Trace.idempotency_key == "concurrent-key")
                )
            ).scalars().all()

        print(f"[concurrent idempotency] n_exceptions={len(exceptions)} distinct_ids={ids} n_rows={len(rows)}")
        assert not exceptions, f"unexpected exceptions: {exceptions}"
        assert ids == {rows[0].id}, "every concurrent retry must resolve to the same single trace id"
        assert len(rows) == 1


class TestAmendTraceIdempotency:
    async def test_identical_retry_with_same_key_returns_the_original_not_a_fork(
        self, session_factory, config, org
    ):
        rate_limiter = make_rate_limiter(config)
        original = await _contribute(session_factory, config, org, "before amendment", "c", "s")

        args = dict(title="amended title", idempotency_key="amend-key-1")
        async with session_scope(session_factory) as session:
            r1 = await crud.amend_trace(session, org, original["id"], config, rate_limiter, **args)
        async with session_scope(session_factory) as session:
            r2 = await crud.amend_trace(session, org, original["id"], config, rate_limiter, **args)

        print(f"[amend idempotency] first id={r1['id']} second id={r2['id']}")

        async with session_scope(session_factory) as session:
            forks = (
                await session.execute(
                    select(Trace).where(Trace.org_id == org, Trace.supersedes_trace_id == original["id"])
                )
            ).scalars().all()

        assert r1["id"] == r2["id"], "retry with the same key must return the original amendment, not a fork"
        assert len(forks) == 1, f"expected exactly 1 amendment after 2 identical-key retries, found {len(forks)}"

    async def test_omitted_key_is_unaffected_and_still_forks(self, session_factory, config, org):
        rate_limiter = make_rate_limiter(config)
        original = await _contribute(session_factory, config, org, "before amendment", "c", "s")

        async with session_scope(session_factory) as session:
            r1 = await crud.amend_trace(session, org, original["id"], config, rate_limiter, title="amended")
        async with session_scope(session_factory) as session:
            r2 = await crud.amend_trace(session, org, original["id"], config, rate_limiter, title="amended")
        assert r1["id"] != r2["id"]

    async def test_same_key_different_trace_id_raises_conflict(self, session_factory, config, org):
        rate_limiter = make_rate_limiter(config)
        original_a = await _contribute(session_factory, config, org, "trace a", "c", "s")
        original_b = await _contribute(session_factory, config, org, "trace b", "c", "s")

        async with session_scope(session_factory) as session:
            await crud.amend_trace(
                session, org, original_a["id"], config, rate_limiter,
                title="amended a", idempotency_key="shared-key",
            )
        with pytest.raises(crud.IdempotencyKeyConflict):
            async with session_scope(session_factory) as session:
                await crud.amend_trace(
                    session, org, original_b["id"], config, rate_limiter,
                    title="amended b", idempotency_key="shared-key",
                )

    async def test_same_key_different_fields_raises_conflict_not_stale_data(
        self, session_factory, config, org
    ):
        rate_limiter = make_rate_limiter(config)
        original = await _contribute(session_factory, config, org, "before amendment", "c", "s")

        async with session_scope(session_factory) as session:
            await crud.amend_trace(
                session, org, original["id"], config, rate_limiter,
                title="first version", idempotency_key="reused-amend-key",
            )
        with pytest.raises(crud.IdempotencyKeyConflict):
            async with session_scope(session_factory) as session:
                await crud.amend_trace(
                    session, org, original["id"], config, rate_limiter,
                    title="a genuinely different edit", idempotency_key="reused-amend-key",
                )

    async def test_20_concurrent_identical_retries_create_exactly_one_amendment(
        self, session_factory, config, org
    ):
        rate_limiter = make_rate_limiter(config)
        original = await _contribute(session_factory, config, org, "before amendment", "c", "s")

        async def _call():
            async with session_scope(session_factory) as session:
                return await crud.amend_trace(
                    session, org, original["id"], config, rate_limiter,
                    title="concurrently amended", idempotency_key="concurrent-amend-key",
                )

        results = await asyncio.gather(*[_call() for _ in range(20)], return_exceptions=True)
        exceptions = [r for r in results if isinstance(r, BaseException)]
        ids = {r["id"] for r in results if not isinstance(r, BaseException)}

        async with session_scope(session_factory) as session:
            forks = (
                await session.execute(
                    select(Trace).where(
                        Trace.org_id == org, Trace.supersedes_trace_id == original["id"]
                    )
                )
            ).scalars().all()

        print(f"[concurrent amend idempotency] n_exceptions={len(exceptions)} distinct_ids={ids} n_forks={len(forks)}")
        assert not exceptions, f"unexpected exceptions: {exceptions}"
        assert ids == {forks[0].id}, "every concurrent retry must resolve to the same single amendment id"
        assert len(forks) == 1


class TestSearchPaginationTiebreak:
    async def test_pagination_with_identical_created_at(self, session_factory, config, org):
        fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        n = 9
        ids: list[str] = []
        async with session_scope(session_factory) as session:
            for i in range(n):
                t = Trace(
                    org_id=org,
                    title=f"tied {i}",
                    context_text=f"c{i}",
                    solution_text=f"s{i}",
                    tags=[],
                    agent_type="code",
                    created_at=fixed,
                )
                session.add(t)
                await session.flush()
                ids.append(t.id)

        seen: list[str] = []
        offset = 0
        limit = 2
        pages = []
        for _ in range(n):
            async with session_scope(session_factory) as session:
                page = await crud.search_traces(session, org, limit=limit, offset=offset)
            pages.append([t["id"] for t in page["traces"]])
            seen.extend(t["id"] for t in page["traces"])
            offset += limit
            if not page["has_more"]:
                break

        missing = set(ids) - set(seen)
        duplicated = [x for x in set(seen) if seen.count(x) > 1]

        print(f"[pagination] inserted={len(ids)} pages={pages}")
        print(f"[pagination] total seen={len(seen)} missing={missing} duplicated={duplicated}")

        assert not duplicated, f"trace id(s) appeared on more than one page: {duplicated}"
        assert not missing, f"trace id(s) never appeared on any page: {missing}"


class TestRetrievalsCounter:
    async def test_concurrent_get_trace_increments_are_not_lost(self, session_factory, config, org):
        trace = await _contribute(session_factory, config, org, "hot trace", "c", "s")
        n = 20

        async def _get():
            async with session_scope(session_factory) as session:
                return await crud.get_trace(session, org, trace["id"])

        results = await asyncio.gather(*[_get() for _ in range(n)])
        assert all(r is not None for r in results)

        async with session_scope(session_factory) as session:
            t = await session.get(Trace, trace["id"])

        print(f"[retrievals] {n} concurrent get_trace calls -> retrievals={t.retrievals}")
        assert t.retrievals == n, f"expected {n} retrievals (atomic UPDATE), got {t.retrievals}"

    async def test_concurrent_search_traces_increments_are_not_lost(self, session_factory, config, org):
        trace = await _contribute(session_factory, config, org, "hot trace 2", "c", "s")
        n = 15

        async def _search():
            async with session_scope(session_factory) as session:
                return await crud.search_traces(session, org, limit=10)

        await asyncio.gather(*[_search() for _ in range(n)])

        async with session_scope(session_factory) as session:
            t = await session.get(Trace, trace["id"])

        print(f"[retrievals via search] {n} concurrent search_traces calls -> retrievals={t.retrievals}")
        assert t.retrievals == n, f"expected {n} retrievals (atomic UPDATE), got {t.retrievals}"


class TestContributeTraceStorageQuotaRace:
    async def test_concurrent_writes_never_exceed_the_cap(self, session_factory, config, org, monkeypatch):
        cap = 5
        monkeypatch.setitem(
            plans.PLANS, "free",
            plans.Plan("free", max_traces=cap, commons_queries_per_month=20,
                       commons_access=True, summary="test"),
        )
        n = 20

        results = await asyncio.gather(
            *[_contribute(session_factory, config, org, f"race {i}", "c", "s") for i in range(n)],
            return_exceptions=True,
        )

        succeeded = [r for r in results if not isinstance(r, BaseException)]
        exceeded = [r for r in results if isinstance(r, plans.EntitlementExceeded)]
        other_exceptions = [
            r for r in results if isinstance(r, BaseException) and not isinstance(r, plans.EntitlementExceeded)
        ]

        async with session_scope(session_factory) as session:
            stored = int(await session.scalar(
                select(func.count()).select_from(Trace).where(Trace.org_id == org)
            ) or 0)

        print(
            f"[storage quota race] cap={cap} n_attempted={n} n_succeeded={len(succeeded)} "
            f"n_entitlement_exceeded={len(exceeded)} n_other_exceptions={len(other_exceptions)} "
            f"stored={stored}"
        )

        assert not other_exceptions, f"unexpected non-quota exceptions: {other_exceptions}"
        assert stored <= cap, f"stored {stored} traces exceeds cap {cap} -- quota race not closed"
        assert len(succeeded) == stored
        assert len(succeeded) + len(exceeded) == n


async def _submit_kb(session_factory, config, org_id, title, context, solution, actor="test"):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        return await crud.submit_kb_entry(
            session, org_id, config, rate_limiter,
            title=title, context_text=context, solution_text=solution,
            tags=[], agent_type="code", actor=actor,
        )


class TestSubmitKbEntryPendingQuotaRace:
    async def test_concurrent_submissions_never_exceed_the_pending_cap(
        self, session_factory, config, org, monkeypatch
    ):
        cap = 5
        monkeypatch.setattr(crud, "MAX_PENDING_SUBMISSIONS_PER_ORG", cap)
        n = 20

        results = await asyncio.gather(
            *[_submit_kb(session_factory, config, org, f"kb race {i}", "c", "s") for i in range(n)],
            return_exceptions=True,
        )

        succeeded = [r for r in results if not isinstance(r, BaseException)]
        rejected = [r for r in results if isinstance(r, crud.TraceRejected)]
        other_exceptions = [
            r for r in results if isinstance(r, BaseException) and not isinstance(r, crud.TraceRejected)
        ]

        async with session_scope(session_factory) as session:
            pending = int(await session.scalar(
                select(func.count()).select_from(crud.KnowledgeBaseSubmission).where(
                    crud.KnowledgeBaseSubmission.org_id == org,
                    crud.KnowledgeBaseSubmission.status == "pending",
                )
            ) or 0)

        print(
            f"[kb pending quota race] cap={cap} n_attempted={n} n_succeeded={len(succeeded)} "
            f"n_rejected={len(rejected)} n_other_exceptions={len(other_exceptions)} pending={pending}"
        )

        assert not other_exceptions, f"unexpected non-quota exceptions: {other_exceptions}"
        assert pending <= cap, f"pending {pending} submissions exceeds cap {cap} -- quota race not closed"
        assert len(succeeded) == pending
        assert len(succeeded) + len(rejected) == n


class TestSubmitKbEntryIdempotency:
    async def test_20_concurrent_identical_submissions_create_exactly_one(
        self, session_factory, config, org
    ):
        rate_limiter = make_rate_limiter(config)
        args = dict(
            title="concurrent kb idempotency probe", context_text="c", solution_text="s",
            idempotency_key="concurrent-kb-key",
        )

        async def _call():
            async with session_scope(session_factory) as session:
                return await crud.submit_kb_entry(session, org, config, rate_limiter, **args)

        results = await asyncio.gather(*[_call() for _ in range(20)], return_exceptions=True)
        exceptions = [r for r in results if isinstance(r, BaseException)]
        ids = {r["id"] for r in results if not isinstance(r, BaseException)}

        async with session_scope(session_factory) as session:
            rows = (
                await session.execute(
                    select(crud.KnowledgeBaseSubmission).where(
                        crud.KnowledgeBaseSubmission.org_id == org,
                        crud.KnowledgeBaseSubmission.idempotency_key == "concurrent-kb-key",
                    )
                )
            ).scalars().all()

        print(
            f"[kb idempotency race] n_exceptions={len(exceptions)} distinct_ids={ids} n_rows={len(rows)}"
        )
        assert not exceptions, f"unexpected exceptions: {exceptions}"
        assert ids == {rows[0].id}, "every concurrent retry must resolve to the same single submission id"
        assert len(rows) == 1
