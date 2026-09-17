"""Regression tests pinning concurrency invariants in hub/crud.py under
real asyncio.gather() concurrency against a live Postgres:

  1. vote_trace: a concurrent race for the first vote on a trace must not
     raise IntegrityError (fixed via an atomic INSERT ... ON CONFLICT
     upsert -- see hub/crud.py:vote_trace); exactly one Vote row must
     survive and trust must stay consistent with it.
  2. contribute_trace: retrying an identical call (simulating a client
     retry after a lost response) with the SAME `idempotency_key` now
     returns the original trace rather than creating a duplicate (fixed
     via an optional idempotency_key param backed by UNIQUE(org_id,
     idempotency_key) -- see hub/crud.py:contribute_trace). Calling
     without a key (the default, matching the pre-idempotency behavior)
     still creates a new row on every call -- that is by design, not a
     remaining gap: there is nothing to deduplicate against without a key
     the caller supplies, and every existing caller that never passes one
     must keep working exactly as before.
  3. search_traces pagination: with tied created_at values, `ORDER BY
     created_at DESC, id DESC` (fixed to include an id tiebreaker) must
     not skip/duplicate rows across pages.
  4. retrievals counter: concurrent get_trace/search_traces calls must
     not lose increments (already atomic via UPDATE ... SET x = x + 1).
  5. amend_trace / contribute_trace storage quota: concurrent writes
     against a plan's max_traces cap must never let the org's trace count
     exceed it (see TestContributeTraceStorageQuotaRace below).
  6. submit_kb_entry: concurrent writes against MAX_PENDING_SUBMISSIONS_PER_ORG
     must never let an org's pending-review queue exceed it (fixed via
     SELECT ... FOR UPDATE on the org row, the same lock _reserve_trace_slot
     already takes -- see TestSubmitKbEntryPendingQuotaRace below).
  7. amend_trace: retrying an identical call with the SAME `idempotency_key`
     now returns the original amendment rather than forking the
     supersession chain (fixed the same way contribute_trace's #2 above is
     -- see TestAmendTraceIdempotency below).
  8. events.deliver_pending: two overlapping sweeps (a slow endpoint makes
     one run past the next cron tick, or an operator runs
     `hub.manage webhook-deliver` by hand while cron also fires) must not
     both select and deliver the SAME due row -- fixed via
     `SELECT ... FOR UPDATE SKIP LOCKED` (see TestDeliverPendingRace
     below), the same lock discipline as #5/#6 above applied to the
     webhook delivery queue.

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

from hub import commons, crud, events, plans
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace, Vote, WebhookDelivery, WebhookEndpoint

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
    """A Knowledge Base entry, seeded directly the way hub/manage.py:
    commons_seed does it -- the only way vote_trace's cross-org path is
    reachable at all (commons_source == "seed"), which is what lets N
    genuinely DISTINCT orgs vote on the same trace below."""
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

    async def test_20_concurrent_votes_from_20_distinct_orgs_all_count(self, session_factory, config, org):
        """Regression test for a real bug distinct from the one above: this
        one is 20 DIFFERENT orgs each casting their own first vote on the
        same Knowledge Base entry at the same time, not one org retrying.
        Each org's Vote row is independent (no INSERT conflict to race), so
        all 20 upserts succeed regardless -- but the trust/commons_votes
        tally computed from a COUNT() and the UPDATE that writes it were
        not atomic with each other: two concurrent voters could each COUNT
        before either had committed, so whichever UPDATE landed second
        overwrote trust/commons_votes with its own stale total, silently
        losing track of the other's already-durable vote. Fixed by locking
        the trace row (SELECT ... FOR UPDATE) before the tally, serializing
        the count-then-write sequence across concurrent voters on the SAME
        trace (see hub/crud.py:vote_trace). Without that lock this test
        reliably found commons_votes < 20 (a real Vote row with no matching
        contribution to the aggregate) within a handful of runs.
        """
        trace_id = await _seed_kb(session_factory, org, "vote race across orgs")
        voter_orgs = await _make_orgs(session_factory, 20, "voter")

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
            # 20 distinct orgs, no conflicting keys -- every vote must land
            # as its own row, and the aggregate on `traces` must match that
            # actual row count exactly, not merely be "close".
            assert len(votes) == 20, f"expected 20 Vote rows (one per org), found {len(votes)}"
            assert t.commons_votes == 20, f"commons_votes lost a concurrent voter's contribution: {t.commons_votes}"
            assert t.trust == pytest.approx(1.0), "all 20 votes were 'up'; trust must reflect all of them"

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


class TestAmendTraceIdempotency:
    """Regression tests for a real bug found by extending the same audit
    that motivated TestContributeTraceIdempotency to amend_trace: unlike
    contribute_trace, amend_trace had NO idempotency_key parameter at all.
    A retried call (a client that timed out waiting for the first
    response) resolved the SAME still-unmutated original and inserted a
    SECOND row superseding it -- forking the supersession chain rather than
    duplicating a sibling trace. Reproduced against a live Postgres before
    the fix: two "identical" amend_trace calls left 2 rows with the same
    supersedes_trace_id, not 1. Fixed the same way contribute_trace already
    is: an optional `idempotency_key` param backed by the same
    UNIQUE(org_id, idempotency_key) constraint on the traces table (see
    hub/crud.py:amend_trace).
    """

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
        """No idempotency_key (the default) must behave exactly as before
        this fix: NULL never conflicts with NULL under the unique
        constraint, so every unkeyed call still creates its own row."""
        rate_limiter = make_rate_limiter(config)
        original = await _contribute(session_factory, config, org, "before amendment", "c", "s")

        async with session_scope(session_factory) as session:
            r1 = await crud.amend_trace(session, org, original["id"], config, rate_limiter, title="amended")
        async with session_scope(session_factory) as session:
            r2 = await crud.amend_trace(session, org, original["id"], config, rate_limiter, title="amended")
        assert r1["id"] != r2["id"]

    async def test_same_key_different_trace_id_raises_conflict(self, session_factory, config, org):
        """Reusing a key against a DIFFERENT original is a different
        request, not a retry -- _amend_request_hash includes trace_id
        precisely so this cannot silently misattribute the reused key's
        amendment to the wrong trace."""
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
        """The realistic failure mode: N retries racing each other, not just
        two sequential calls. Exercises the IntegrityError/rollback/refetch
        path in amend_trace, not just the up-front SELECT -- the same path
        TestContributeTraceIdempotency's concurrent test exercises for
        contribute_trace."""
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


# --- 6. submit_kb_entry pending-submission cap ---------------------------


async def _submit_kb(session_factory, config, org_id, title, context, solution, actor="test"):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        return await crud.submit_kb_entry(
            session, org_id, config, rate_limiter,
            title=title, context_text=context, solution_text=solution,
            tags=[], agent_type="code", actor=actor,
        )


class TestSubmitKbEntryPendingQuotaRace:
    """Regression test for the same class of bug TestContributeTraceStorageQuotaRace
    pins for contribute_trace, found in submit_kb_entry's sibling check:
    MAX_PENDING_SUBMISSIONS_PER_ORG was enforced with a plain COUNT(*) of
    status='pending' rows followed later by an INSERT, with nothing
    serializing the two across concurrent callers for the same org. N
    coroutines racing when the org is one submission away from the cap
    could each COUNT before any of the others' INSERT was visible, so all N
    pass a check only one of them should have -- the queue grows past
    MAX_PENDING_SUBMISSIONS_PER_ORG with no error ever raised, defeating the
    docstring's own stated purpose ("Bounds how large the operator's review
    queue can be forced to grow by one org"). Fixed the same way: SELECT
    ... FOR UPDATE on the org's own row before the count (see
    hub/crud.py:submit_kb_entry), the identical lock _reserve_trace_slot
    already takes for the same reason.
    """

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


# --- 8. submit_kb_entry idempotency-key race ------------------------------
#
# contribute_trace and amend_trace both have a dedicated 20-way concurrent
# test exercising their IntegrityError/rollback/refetch fallback path
# (TestContributeTraceIdempotency / TestAmendTraceIdempotency above) --
# submit_kb_entry's identical idempotency_key mechanism (same UNIQUE
# constraint pattern, same up-front-SELECT-then-INSERT-with-fallback
# shape, hub/crud.py:submit_kb_entry) had no concurrency test of its own,
# a coverage gap surfaced by running `pytest --cov` over hub/crud.py and
# checking every uncovered line for a bug hiding behind a missing test --
# none were found, but this specific fallback path (hub/crud.py's own
# `except IntegrityError: ... return _submission_idempotent_replay_or_conflict(...)`)
# was one of the ones that stayed uncovered, and it is exactly the kind of
# path this session's own earlier audits (TestContributeTraceIdempotency's
# docstring) treat as worth pinning under real concurrency rather than
# trusting by inspection alone.


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


# --- 8. events.deliver_pending double-delivery race -----------------------


class TestDeliverPendingRace:
    """Regression test for a real bug: `deliver_pending`'s SELECT of due
    rows took no row lock, unlike every other shared-counter path this
    audit file covers. Two overlapping sweeps (a slow endpoint makes one
    run past the next cron tick, or an operator runs
    `hub.manage webhook-deliver` by hand while cron also fires) could both
    select the SAME due delivery, both POST it to the customer's endpoint
    concurrently, and have the loser's `attempts`/`status` write silently
    discarded. Fixed with `SELECT ... FOR UPDATE SKIP LOCKED`
    (hub/events.py:deliver_pending), so concurrent sweeps get disjoint
    rows instead."""

    async def test_concurrent_sweeps_never_deliver_the_same_row_twice(
        self, session_factory, org
    ):
        n = 20
        async with session_scope(session_factory) as session:
            endpoint = WebhookEndpoint(
                org_id=org, url="https://example.invalid/hook",
                events=list(events.EVENT_NAMES),
            )
            session.add(endpoint)
            await session.flush()
            endpoint_id = endpoint.id
            for i in range(n):
                session.add(WebhookDelivery(
                    org_id=org, endpoint_id=endpoint_id, event_type="trace.created",
                    payload={"trace_id": f"t{i}"},
                ))

        delivered_ids: list[str] = []

        async def _fake_transport(url, body, headers):
            # A single event loop interleaves these coroutines only at
            # `await` points, so a call recorded here reflects a delivery
            # that already passed this sweep's row lock -- appending to a
            # shared list needs no additional lock of its own.
            delivered_ids.append(headers["X-CommonTrace-Event-Id"])

        async def _sweep():
            async with session_scope(session_factory) as session:
                return await events.deliver_pending(
                    session, _fake_transport, signing_key="test-key", limit=100,
                )

        results = await asyncio.gather(*[_sweep() for _ in range(5)])

        print(
            f"[deliver_pending race] n_queued={n} n_delivered_calls={len(delivered_ids)} "
            f"n_distinct_delivered={len(set(delivered_ids))} "
            f"per_sweep_attempted={[r.attempted for r in results]}"
        )
        # The invariant the row lock exists to guarantee: every queued
        # delivery is attempted, and none is attempted more than once
        # across all concurrent sweeps combined.
        assert len(delivered_ids) == len(set(delivered_ids)), (
            "the same delivery was handed to the transport more than once "
            "across concurrent sweeps -- FOR UPDATE SKIP LOCKED did not "
            "give them disjoint rows"
        )
        assert len(delivered_ids) == n
        assert sum(r.attempted for r in results) == n
