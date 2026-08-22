"""Regression tests pinning four concurrency invariants in hub/crud.py
under real asyncio.gather() concurrency against a live Postgres:

  1. vote_trace: a concurrent race for the first vote on a trace must not
     raise IntegrityError (fixed via an atomic INSERT ... ON CONFLICT
     upsert -- see hub/crud.py:vote_trace); exactly one Vote row must
     survive and trust must stay consistent with it.
  2. contribute_trace: retrying an identical call (simulating a client
     retry after a lost response) still creates a second Trace row today
     -- contribute_trace has no idempotency mechanism. This test pins
     that as a known, not-yet-implemented gap (see task: add
     idempotency_key to contribute_trace) rather than silently accepting
     duplication as correct.
  3. search_traces pagination: with tied created_at values, `ORDER BY
     created_at DESC, id DESC` (fixed to include an id tiebreaker) must
     not skip/duplicate rows across pages.
  4. retrievals counter: concurrent get_trace/search_traces calls must
     not lose increments (already atomic via UPDATE ... SET x = x + 1).

Run with: HUB_TEST_DATABASE_URL=... pytest -s -v \
    hub/tests/test_concurrency_audit.py
(-s to see the printed per-run diagnostics)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from hub import crud, plans
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


# --- 1. vote_trace concurrency ------------------------------------------


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
        """Regression test for a real bug: vote_trace used to do a separate
        SELECT-existing-vote then INSERT-or-UPDATE, which is not atomic --
        when N coroutines raced with no existing Vote row, more than one
        could see `existing is None` and attempt an INSERT, and the loser(s)
        got an uncaught IntegrityError against uq_votes_trace_org propagating
        out of vote_trace (reproduced: up to 8/20 concurrent calls failed
        this way). Fixed by making the write a single atomic
        `INSERT ... ON CONFLICT (trace_id, org_id) DO UPDATE` (see
        hub/crud.py:vote_trace) -- the race is closed at the database level,
        so no caller-side exception handling can paper over it.
        """
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

            # The atomic upsert means every one of the 20 concurrent calls
            # must succeed -- no IntegrityError, no exception of any kind.
            assert not exceptions, f"unexpected exceptions under concurrent voting: {exceptions}"
            assert len(succeeded) == 20
            # uq_votes_trace_org + the org-scoped trace lookup means at most
            # one Vote row can ever exist for this (trace_id, org_id) pair.
            assert len(votes) == 1, f"expected exactly 1 Vote row, found {len(votes)}"
            # trust must be internally consistent with the single surviving vote
            expected_trust = 1.0 if votes[0].vote_type == "up" else 0.0
            assert t.trust == pytest.approx(expected_trust)

    async def test_cross_org_cannot_vote_on_a_trace_it_does_not_own(self, session_factory, config, org):
        """Sanity-check the premise: vote_trace scopes the trace lookup to
        Trace.org_id == org_id, so a second org can never insert a second
        Vote row for the same trace_id -- there is no reachable path to a
        multi-row uq_votes_trace_org group for one trace_id."""
        trace = await _contribute(session_factory, config, org, "t", "c", "s")
        async with session_scope(session_factory) as session:
            other_org = Organization(name="other-org")
            session.add(other_org)
            await session.flush()
            other_org_id = other_org.id

        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, other_org_id, trace["id"], "up")
        assert result is None  # 404-shaped, not a vote recorded under the wrong org


# --- 2. contribute_trace idempotency ------------------------------------


class TestContributeTraceIdempotency:
    """Regression tests for a real bug: contribute_trace had no way to
    recognize a retried call as "the same write" (server-generated uuid4
    id, no key), so a client retrying after a lost response always got a
    duplicate trace. Fixed by an optional `idempotency_key` param backed by
    UNIQUE(org_id, idempotency_key) -- see hub/crud.py:contribute_trace.
    """

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
        """No idempotency_key (the default) must behave exactly as before:
        NULL never conflicts with NULL under the unique constraint."""
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
        """The realistic failure mode: N retries racing each other (not just
        two sequential calls) must still produce exactly one logical write.
        Exercises the IntegrityError/rollback/refetch path in
        contribute_trace, not just the up-front SELECT."""
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


# --- 3. search_traces pagination stability with tied created_at ---------


class TestSearchPaginationTiebreak:
    async def test_pagination_with_identical_created_at(self, session_factory, config, org):
        """Forces every row's created_at to the SAME timestamp (bypassing
        the _now default), then pages through with limit=2 and checks that
        `ORDER BY created_at DESC, id DESC` (crud.py's `else` branch) never
        skips or repeats a row. Before the id tiebreaker was added, ties
        were only observed to page stably because Postgres happened to
        return a consistent physical row order for this read-only,
        no-interleaved-write scenario -- not because the query guaranteed
        it. The explicit id tiebreaker makes the order actually
        deterministic rather than incidentally stable.
        """
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
                    created_at=fixed,  # bypass default=_now -- force an exact tie
                )
                session.add(t)
                await session.flush()
                ids.append(t.id)

        seen: list[str] = []
        offset = 0
        limit = 2
        pages = []
        for _ in range(n):  # generous upper bound on page count
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


# --- 4. retrievals counter: atomic UPDATE vs lost-update -----------------


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
        """search_traces bumps retrievals for every row it returns via the
        same UPDATE ... SET retrievals = retrievals + 1 pattern -- confirm
        it too survives concurrent callers."""
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


# --- 5. contribute_trace storage quota: count-then-insert race -----------


class TestContributeTraceStorageQuotaRace:
    """Regression test for a real bug: the max_traces check was a plain
    COUNT(*) followed later by an INSERT, with nothing serializing the two
    across concurrent callers. N coroutines racing when the org is one
    trace away from its cap could each COUNT before any of the others'
    INSERT was visible, so all N could pass a check only one of them
    should have -- stored trace count ends up above max_traces with no
    error ever raised. Fixed with `SELECT ... FOR UPDATE` on the org's own
    row before the count, serializing this org's concurrent writes without
    touching any other org's throughput (hub/crud.py:contribute_trace).
    """

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
        # The invariant the row lock exists to guarantee: stored count can
        # never exceed the cap, no matter how many callers raced for it.
        assert stored <= cap, f"stored {stored} traces exceeds cap {cap} -- quota race not closed"
        assert len(succeeded) == stored
        assert len(succeeded) + len(exceeded) == n
