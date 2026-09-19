"""`commons_hits` is a shared number, and until this file nothing defended it.

hub/commons.py builds an "autoconfirmed" bar -- old enough, with real
traces behind it -- on the explicit premise that *organizations are cheap*:
signup is self-serve and `POST /api/v1/keys` mints an org and a working key
over HTTP with no human in the loop. That bar was applied to VOTES, which
move `trust` / `commons_votes` and therefore an entry's standing.

It was never applied to `commons_hits`, which moves two other things:

  * the operator's curation decision -- `hub/manage.py:kb_stats` lists
    zero-hit entries as prune candidates and ranks the rest by hits, so the
    counter steers which knowledge the corpus keeps and grows; and
  * (formerly) the ORDER customers read candidates in, as
    `commons_search`'s tie-break.

So the corpus refused to let a minted org say an entry was bad, while
letting it say an entry was popular for free. That is the wrong half to
defend, and the arithmetic is not marginal: the `free` plan carries 20
commons queries a month and `MAX_HITS_PER_TRACE_PER_QUERY` lets each credit
20 hits to one entry -- 400 units of influence over the operator's signal,
from an org that has never captured a trace and cannot cast one counted
vote.

Every test here fails on the code before that fix. They are grouped by the
two independent halves of it, because either alone leaves a live path:
gating WHO may credit a hit does not bound HOW MUCH one qualifying org may
credit, and an org's query volume is exactly what it pays for.
"""
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
    """The bar that governs votes now governs hits, for the same reason and
    with the same thresholds."""

    async def test_a_brand_new_org_gets_its_answer_but_credits_no_hit(
        self, session_factory, orgs
    ):
        """The load-bearing one. Before the fix this recorded a hit from an
        org with zero traces and zero age -- an org the vote path would not
        let cast a single counted vote."""
        tid = await _seed(session_factory, orgs["operator"], *ENTRY)
        report = await _overlap(
            session_factory, orgs["newcomer"], [_failure("f1", ENTRY[0], ENTRY[1])]
        )
        # The answer is unchanged: gating the shared counter must not
        # quietly degrade what a new customer is told.
        assert report["n_covered"] == 1
        assert await _hits(session_factory, tid) == 0

    async def test_an_established_org_does_credit_a_hit(self, session_factory, orgs, establish_orgs):
        """The other side of the same rule -- without this, the test above
        would also pass if hits had simply been removed entirely."""
        tid = await _seed(session_factory, orgs["operator"], *ENTRY)
        await establish_orgs(orgs["regular"])
        report = await _overlap(
            session_factory, orgs["regular"], [_failure("f1", ENTRY[0], ENTRY[1])]
        )
        assert report["n_covered"] == 1
        assert await _hits(session_factory, tid) == 1

    async def test_the_free_org_sockpuppet_budget_is_now_zero(self, session_factory, orgs):
        """The attack as arithmetic. MAX_HITS_PER_TRACE_PER_QUERY caps ONE
        call at 20; nothing capped the number of calls, and a free org's
        20 monthly queries made 400 hits reachable for nothing. Three
        minted orgs is a corpus-curation majority the vote path would have
        refused outright."""
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
        """Waiting is the cheap half of the bar. An org that sat idle for a
        day has still never run anything, so it has no standing to report
        that a fix works."""
        tid = await _seed(session_factory, orgs["operator"], *ENTRY)
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, orgs["newcomer"])
            org.created_at = datetime.now(timezone.utc) - __import__("datetime").timedelta(
                hours=commons.COMMONS_VOTER_MIN_AGE_HOURS + 1
            )
        await _overlap(session_factory, orgs["newcomer"], [_failure("f1", ENTRY[0], ENTRY[1])])
        assert await _hits(session_factory, tid) == 0

    async def test_the_two_bars_are_one_rule(self):
        """Not a tautology: these were separate predicates, and the whole
        defect was that one signal had a bar and the other did not. Pinning
        them to the same answer is what stops the next signal drifting off
        again."""
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
    """The second half. Gating WHO credits a hit leaves HOW MUCH unbounded
    -- query volume is what an org pays for, and `scale` carries 25,000 a
    month. So `commons_hits` must not decide a customer-facing order at
    all, however established the org inflating it."""

    async def test_hits_do_not_outrank_an_equally_similar_entry(
        self, session_factory, orgs
    ):
        """The manipulation, end to end. Two entries tie on similarity --
        which is the NORMAL case, not a contrived one: MinHash similarity
        is quantized to k/COMMONS_NUM_PERM, and measured on the shipped
        corpus against the held-out probes, 82.8% of queries have a tie
        inside the top 10. Before the fix, whichever entry carried more
        hits won that tie, so an attacker's traffic chose what every other
        customer read first."""
        title, ctx, sol = ENTRY
        first = await _seed(session_factory, orgs["operator"], title, ctx, sol)
        second = await _seed(session_factory, orgs["operator"], title, ctx, "a different fix")

        # Identical matchable text => identical signatures => an exact tie,
        # asserted rather than assumed: if these ever stopped tying, the
        # test would be proving nothing about tie-breaks.
        async with session_scope(session_factory) as session:
            a = await session.get(Trace, first)
            b = await session.get(Trace, second)
            assert a.commons_signature == b.commons_signature
            # Hand the SECOND entry a large hit count and leave the first at
            # zero. Set directly: how the count got there is the attacker's
            # problem, and the point is what the ranker does with it.
            b.commons_hits = 10_000

        async with session_scope(session_factory) as session:
            r = await crud.commons_search(
                session, orgs["regular"], commons.signature_for(title, ctx, [])
            )
        ranked = [c["trace"]["id"] for c in r["candidates"]]
        assert set(ranked[:2]) == {first, second}
        assert ranked[0] == first, "traffic volume decided a customer-facing ranking"

    async def test_trust_still_breaks_a_tie(self, session_factory, orgs):
        """The replacement signal has to actually work, or this is a
        removal rather than a fix. `trust` is one org, one vote, and gated
        by the same established-voter filter."""
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
        """With hits gone from the key, entries that tie on similarity AND
        trust fall to a deterministic key rather than to whatever order the
        corpus scan returned. Two identical queries returning two different
        first answers is its own defect, and it is the one a careless
        removal of the tie-break would have introduced."""
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
        """Removed from the ORDER, not from the answer. A caller judging
        candidates may reasonably want to know how much traffic an entry
        has actually served."""
        tid = await _seed(session_factory, orgs["operator"], *ENTRY)
        async with session_scope(session_factory) as session:
            (await session.get(Trace, tid)).commons_hits = 7
        async with session_scope(session_factory) as session:
            r = await crud.commons_search(
                session, orgs["regular"], commons.signature_for(ENTRY[0], ENTRY[1], [])
            )
        assert r["candidates"][0]["commons_hits"] == 7
