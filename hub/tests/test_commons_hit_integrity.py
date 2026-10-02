from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from hub import commons, crud
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("operator", "newcomer", "regular"):
            o = Organization(name=name)
            session.add(o)
            await session.flush()
            made[name] = o.id
        return made


async def _seed(session_factory, operator_org_id, title, context, solution, tags=None):
    tags = tags or []
    async with session_scope(session_factory) as session:
        trace = Trace(
            org_id=operator_org_id,
            title=title,
            context_text=context,
            solution_text=solution,
            tags=tags,
            agent_type="code",
            shared_with_commons=True,
            shared_at=datetime.now(timezone.utc),
            shared_rationale="test fixture: operator-curated substrate knowledge",
            commons_signature=commons.signature_for(title, context, tags),
            commons_source="seed",
        )
        session.add(trace)
        await session.flush()
        return trace.id


def _failure(label, title, context, tags=None):
    return {
        "label": label,
        "signature": commons.signature_for(title, context, tags or []),
    }


async def _overlap(session_factory, org_id, failures):
    async with session_scope(session_factory) as session:
        return await crud.commons_overlap(session, org_id, failures)


async def _hits(session_factory, trace_id):
    async with session_scope(session_factory) as session:
        return (await session.get(Trace, trace_id)).commons_hits


ENTRY = (
    "Stripe webhook delivered more than once",
    "the payment provider re-delivers a webhook after a timeout so the handler runs twice",
    "persist the provider event id and check it before any side effect",
)


class TestOnlyAnEstablishedOrgMovesTheQualitySignal:
    async def test_a_brand_new_org_gets_its_answer_but_credits_no_hit(
        self, session_factory, orgs
    ):
        tid = await _seed(session_factory, orgs["operator"], *ENTRY)
        report = await _overlap(
            session_factory, orgs["newcomer"], [_failure("f1", ENTRY[0], ENTRY[1])]
        )
        assert report["n_covered"] == 1
        assert await _hits(session_factory, tid) == 0

    async def test_an_established_org_does_credit_a_hit(self, session_factory, orgs, establish_orgs):
        tid = await _seed(session_factory, orgs["operator"], *ENTRY)
        await establish_orgs(orgs["regular"])
        report = await _overlap(
            session_factory, orgs["regular"], [_failure("f1", ENTRY[0], ENTRY[1])]
        )
        assert report["n_covered"] == 1
        assert await _hits(session_factory, tid) == 1

    async def test_the_free_org_sockpuppet_budget_is_now_zero(self, session_factory, orgs):
        tid = await _seed(session_factory, orgs["operator"], *ENTRY)
        batch = [
            _failure(f"f{i}", ENTRY[0], ENTRY[1])
            for i in range(commons.MAX_HITS_PER_TRACE_PER_QUERY)
        ]
        for _ in range(3):
            await _overlap(session_factory, orgs["newcomer"], batch)
        assert await _hits(session_factory, tid) == 0

    async def test_age_alone_does_not_qualify_an_org_with_no_traces(
        self, session_factory, orgs
    ):
        tid = await _seed(session_factory, orgs["operator"], *ENTRY)
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, orgs["newcomer"])
            org.created_at = datetime.now(timezone.utc) - __import__("datetime").timedelta(
                hours=commons.COMMONS_VOTER_MIN_AGE_HOURS + 1
            )
        await _overlap(session_factory, orgs["newcomer"], [_failure("f1", ENTRY[0], ENTRY[1])])
        assert await _hits(session_factory, tid) == 0

    async def test_the_two_bars_are_one_rule(self):
        cases = [
            (0, None),
            (commons.COMMONS_VOTER_MIN_TRACES, None),
            (0, datetime.now(timezone.utc)),
            (commons.COMMONS_VOTER_MIN_TRACES - 1, datetime(2000, 1, 1, tzinfo=timezone.utc)),
            (commons.COMMONS_VOTER_MIN_TRACES, datetime(2000, 1, 1, tzinfo=timezone.utc)),
        ]
        for traces, created in cases:
            kw = {"trace_count": traces, "org_created_at": created}
            assert commons.vote_counts_toward_standing(
                **kw
            ) == commons.hit_counts_toward_quality_signal(**kw) == commons.org_is_established(**kw)


class TestTrafficDoesNotOrderWhatCustomersRead:
    async def test_hits_do_not_outrank_an_equally_similar_entry(
        self, session_factory, orgs
    ):
        title, ctx, sol = ENTRY
        first = await _seed(session_factory, orgs["operator"], title, ctx, sol)
        second = await _seed(session_factory, orgs["operator"], title, ctx, "a different fix")

        async with session_scope(session_factory) as session:
            a = await session.get(Trace, first)
            b = await session.get(Trace, second)
            assert a.commons_signature == b.commons_signature
            b.commons_hits = 10_000

        async with session_scope(session_factory) as session:
            r = await crud.commons_search(
                session, orgs["regular"], commons.signature_for(title, ctx, [])
            )
        ranked = [c["trace"]["id"] for c in r["candidates"]]
        assert set(ranked[:2]) == {first, second}
        assert ranked[0] == first, "traffic volume decided a customer-facing ranking"

    async def test_trust_still_breaks_a_tie(self, session_factory, orgs):
        title, ctx, sol = ENTRY
        low = await _seed(session_factory, orgs["operator"], title, ctx, sol)
        high = await _seed(session_factory, orgs["operator"], title, ctx, "a different fix")
        async with session_scope(session_factory) as session:
            (await session.get(Trace, low)).trust = 0.6
            (await session.get(Trace, high)).trust = 0.9

        async with session_scope(session_factory) as session:
            r = await crud.commons_search(
                session, orgs["regular"], commons.signature_for(title, ctx, [])
            )
        assert [c["trace"]["id"] for c in r["candidates"]][0] == high

    async def test_the_order_is_stable_across_identical_queries(
        self, session_factory, orgs
    ):
        title, ctx, sol = ENTRY
        for i in range(5):
            await _seed(session_factory, orgs["operator"], title, ctx, f"fix {i}")
        seen = set()
        for _ in range(4):
            async with session_scope(session_factory) as session:
                r = await crud.commons_search(
                    session, orgs["regular"], commons.signature_for(title, ctx, [])
                )
            seen.add(tuple(c["trace"]["id"] for c in r["candidates"]))
        assert len(seen) == 1, f"ranking was not stable across identical queries: {seen}"

    async def test_hits_are_still_reported_on_each_candidate(self, session_factory, orgs):
        tid = await _seed(session_factory, orgs["operator"], *ENTRY)
        async with session_scope(session_factory) as session:
            (await session.get(Trace, tid)).commons_hits = 7
        async with session_scope(session_factory) as session:
            r = await crud.commons_search(
                session, orgs["regular"], commons.signature_for(ENTRY[0], ENTRY[1], [])
            )
        assert r["candidates"][0]["commons_hits"] == 7
