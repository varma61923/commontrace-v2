from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

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
        original = await _contribute(session_factory, config, org)
        amended = await _amend(session_factory, config, org, original["id"], title="v2")

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, amended["id"])
        assert row.superseded_at is None
        assert row.superseded_by_trace_id is None

    async def test_the_two_writes_are_atomic(self, session_factory, config, org):
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
        assert r2.superseded_by_trace_id == v3["id"]
        assert r3.superseded_by_trace_id is None


class TestSearchStopsOfferingStaleTraces:
    async def test_searching_old_wording_no_longer_returns_the_stale_original(
        self, session_factory, config, org
    ):
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
        trace = await _contribute(
            session_factory, config, org, title="wwvut never amended marker"
        )
        async with session_scope(session_factory) as session:
            result = await crud.search_traces(session, org, query="wwvut never amended")
        assert trace["id"] in [t["id"] for t in result["traces"]]


class TestGetTraceStillReturnsSupersededTraces:
    async def test_fetching_a_superseded_trace_by_id_still_works(
        self, session_factory, config, org
    ):
        original = await _contribute(session_factory, config, org)
        amended = await _amend(session_factory, config, org, original["id"], title="v2")

        async with session_scope(session_factory) as session:
            fetched = await crud.get_trace(session, org, original["id"])

        assert fetched is not None
        assert fetched["superseded_by_trace_id"] == amended["id"]
        assert fetched["superseded_at"]

    async def test_the_wire_shape_is_empty_string_for_a_never_amended_trace(
        self, session_factory, config, org
    ):
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
        assert replay["id"] == first["id"]

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, original["id"])
        assert row.superseded_by_trace_id == first["id"]


class TestTheKnowledgeBaseSurfaceGetsTheSameFix:
    async def test_a_superseded_shared_trace_is_no_longer_commons_visible(
        self, session_factory, config, org
    ):
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


class TestForkedAmendmentsAllStayLive:
    async def test_both_forks_of_the_same_parent_are_independently_live(
        self, session_factory, config, org
    ):
        v1 = await _contribute(session_factory, config, org, title="parent")
        fork_a = await _amend(session_factory, config, org, v1["id"], title="fork a")
        fork_b = await _amend(session_factory, config, org, v1["id"], title="fork b")

        async with session_scope(session_factory) as session:
            row_a = await session.get(Trace, fork_a["id"])
            row_b = await session.get(Trace, fork_b["id"])
            parent = await session.get(Trace, v1["id"])
        assert row_a.superseded_at is None
        assert row_b.superseded_at is None
        assert parent.superseded_at is not None
