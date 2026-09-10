"""A superseded trace says so, on its own row -- and search stops offering it.

amend_trace's own docstring has always correctly described the chain as
"supersedes, does not mutate": it creates a NEW Trace and leaves the one
it amends untouched. What "untouched" meant in practice, before this file
existed, was that search_traces had NOTHING on the original row it could
filter on -- `supersedes_trace_id` only ever points backward, from an
amendment to what it replaced, never forward. A search could therefore
return the STALE original, sometimes in place of its correction, whenever
the old wording happened to rank higher on text relevance than the new
one. Reproduced against a live Hub before hub/models.py:Trace.superseded_at
existed: amend a trace, search for wording that exists only in the old
version, get the old version back.

The fix is the idea, not the code, adapted from Zep/Graphiti's bi-temporal
fact model: a superseded fact is invalidated -- a timestamped, queryable
event on the fact itself -- never deleted. `TestSearchStopsOfferingStaleTraces`
is the direct regression test for the reproduced bug; the rest of this
file pins the surrounding contract (atomicity with the amending INSERT,
"invalidated not deleted" via get_trace, the wire shape, idempotent
replay, and the same fix applied to the cross-org Knowledge Base surface,
where the same staleness would otherwise mislead every OTHER org instead
of just the one that wrote it).
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from hub import crud
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="bitemporal-org")
        session.add(o)
        await session.flush()
        return o.id


async def _contribute(session_factory, config, org_id, **overrides):
    rate_limiter = make_rate_limiter(config)
    fields = {
        "title": "Deploy fails when config file is missing",
        "context_text": "startup crashes with FileNotFoundError on boot",
        "solution_text": "check for the file and log a clear error instead of crashing",
        "tags": ["deploy"],
        "agent_type": "code",
    }
    fields.update(overrides)
    async with session_scope(session_factory) as session:
        return await crud.contribute_trace(
            session, org_id, config, rate_limiter, actor="test", **fields
        )


async def _amend(session_factory, config, org_id, trace_id, **overrides):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        return await crud.amend_trace(
            session, org_id, trace_id, config, rate_limiter, actor="test", **overrides
        )


class TestTheOriginalRecordsItsOwnSupersession:
    async def test_amending_sets_both_columns_on_the_row_being_amended(
        self, session_factory, config, org
    ):
        original = await _contribute(session_factory, config, org)
        amended = await _amend(
            session_factory, config, org, original["id"],
            title="Deploy fails when the config file is missing (rewritten)",
        )

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, original["id"])
        assert row.superseded_at is not None
        assert row.superseded_by_trace_id == amended["id"]

    async def test_the_new_head_carries_no_supersession_of_its_own(
        self, session_factory, config, org
    ):
        """The amendment is the current head of the chain -- nothing
        supersedes IT yet -- until some later amendment does."""
        original = await _contribute(session_factory, config, org)
        amended = await _amend(session_factory, config, org, original["id"], title="v2")

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, amended["id"])
        assert row.superseded_at is None
        assert row.superseded_by_trace_id is None

    async def test_the_two_writes_are_atomic(self, session_factory, config, org):
        """A reader can never observe a new head with no superseded
        original, or a superseded original with no new head -- both land
        in the same flush as the same INSERT. Checked here by reading both
        rows back together after the call returns, rather than trusting
        that no interleaving is possible."""
        original = await _contribute(session_factory, config, org)
        amended = await _amend(session_factory, config, org, original["id"], title="v2")

        async with session_scope(session_factory) as session:
            orig_row = await session.get(Trace, original["id"])
            new_row = await session.get(Trace, amended["id"])
        assert orig_row.superseded_by_trace_id == new_row.id
        assert new_row.supersedes_trace_id == orig_row.id

    async def test_a_chain_of_two_amendments_links_correctly_in_both_directions(
        self, session_factory, config, org
    ):
        v1 = await _contribute(session_factory, config, org)
        v2 = await _amend(session_factory, config, org, v1["id"], title="v2")
        v3 = await _amend(session_factory, config, org, v2["id"], title="v3")

        async with session_scope(session_factory) as session:
            r1 = await session.get(Trace, v1["id"])
            r2 = await session.get(Trace, v2["id"])
            r3 = await session.get(Trace, v3["id"])
        assert r1.superseded_by_trace_id == v2["id"]
        assert r2.superseded_by_trace_id == v3["id"]  # v2 is superseded too, not just v1
        assert r3.superseded_by_trace_id is None  # v3 is the current head


class TestSearchStopsOfferingStaleTraces:
    """The direct regression test for the reproduced bug."""

    async def test_searching_old_wording_no_longer_returns_the_stale_original(
        self, session_factory, config, org
    ):
        """The exact reproduction: title/context text unique to the OLD
        version, amended to different wording, then searched for the old
        phrase. Before Trace.superseded_at existed this returned the stale
        original -- the only row that still textually matched -- which is
        precisely the case an agent must never be handed: confidently
        wrong, in its own words, from its own history."""
        original = await _contribute(
            session_factory, config, org,
            title="zzqrx sentinel marker phrase alpha",
            context_text="irrelevant",
        )
        await _amend(
            session_factory, config, org, original["id"],
            title="a completely different, unrelated title",
        )

        async with session_scope(session_factory) as session:
            result = await crud.search_traces(session, org, query="zzqrx sentinel marker phrase")

        ids = [t["id"] for t in result["traces"]]
        assert original["id"] not in ids, (
            "search returned the superseded, stale original -- the exact "
            "regression this file exists to catch"
        )

    async def test_the_current_head_is_still_findable_by_its_own_wording(
        self, session_factory, config, org
    ):
        original = await _contribute(session_factory, config, org)
        amended = await _amend(
            session_factory, config, org, original["id"],
            title="qqzyx unique marker for the amended version",
        )

        async with session_scope(session_factory) as session:
            result = await crud.search_traces(session, org, query="qqzyx unique marker")

        ids = [t["id"] for t in result["traces"]]
        assert amended["id"] in ids
        assert original["id"] not in ids

    async def test_an_unamended_trace_is_unaffected(self, session_factory, config, org):
        """The common case -- most traces are never amended -- must not
        regress: superseded_at is NULL by default, so the new predicate
        excludes nothing for a trace that was never touched."""
        trace = await _contribute(
            session_factory, config, org, title="wwvut never amended marker"
        )
        async with session_scope(session_factory) as session:
            result = await crud.search_traces(session, org, query="wwvut never amended")
        assert trace["id"] in [t["id"] for t in result["traces"]]


class TestGetTraceStillReturnsSupersededTraces:
    """Zep's "invalidated, never deleted": a superseded trace is excluded
    from search RESULTS, not erased. A caller who already has the id --
    from an old page, an old citation, an audit trail -- can still fetch
    it and see, from the wire fields, what replaced it."""

    async def test_fetching_a_superseded_trace_by_id_still_works(
        self, session_factory, config, org
    ):
        original = await _contribute(session_factory, config, org)
        amended = await _amend(session_factory, config, org, original["id"], title="v2")

        async with session_scope(session_factory) as session:
            fetched = await crud.get_trace(session, org, original["id"])

        assert fetched is not None
        assert fetched["superseded_by_trace_id"] == amended["id"]
        assert fetched["superseded_at"]  # non-empty ISO timestamp string

    async def test_the_wire_shape_is_empty_string_for_a_never_amended_trace(
        self, session_factory, config, org
    ):
        """Matching supersedes_trace_id's own established convention
        (empty string, not null/absent) for "there is no such trace"."""
        trace = await _contribute(session_factory, config, org)
        async with session_scope(session_factory) as session:
            fetched = await crud.get_trace(session, org, trace["id"])
        assert fetched["superseded_by_trace_id"] == ""
        assert fetched["superseded_at"] == ""


class TestIdempotentReplayDoesNotDoubleSupersede:
    async def test_a_replayed_amend_call_does_not_change_the_original_again(
        self, session_factory, config, org
    ):
        original = await _contribute(session_factory, config, org)
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            first = await crud.amend_trace(
                session, org, original["id"], config, rate_limiter,
                title="v2", actor="test", idempotency_key="k1",
            )
        async with session_scope(session_factory) as session:
            replay = await crud.amend_trace(
                session, org, original["id"], config, rate_limiter,
                title="v2", actor="test", idempotency_key="k1",
            )
        assert replay["id"] == first["id"]  # same amendment returned, not a new one

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, original["id"])
        assert row.superseded_by_trace_id == first["id"]  # unchanged by the replay


class TestTheKnowledgeBaseSurfaceGetsTheSameFix:
    """The cross-org version of the same bug: a shared trace an org later
    amends does not automatically re-share the correction (amend_trace
    does not carry shared_with_commons onto the new row -- that is a
    separate, explicit decision this fix does not make). Left unfiltered,
    the stale original would keep being served to every OTHER org from
    the Knowledge Base indefinitely -- worse than the per-org case, since
    there it only misleads the org that wrote it."""

    async def test_a_superseded_shared_trace_is_no_longer_commons_visible(
        self, session_factory, config, org
    ):
        from sqlalchemy import select

        trace = await _contribute(session_factory, config, org)
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, trace["id"])
            row.shared_with_commons = True
            row.commons_source = "seed"

        await _amend(session_factory, config, org, trace["id"], title="v2")

        async with session_factory() as session:
            stmt = select(Trace.id).where(Trace.id == trace["id"], *crud.commons_visible())
            visible = (await session.execute(stmt)).scalar_one_or_none()
        assert visible is None, "a superseded trace is still served from the Knowledge Base"
