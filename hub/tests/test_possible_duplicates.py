from __future__ import annotations

import pytest
import pytest_asyncio

from hub import crud
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="possible-duplicates-org")
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


class TestNearDuplicatesAreSurfaced:
    async def test_a_near_duplicate_trace_is_flagged(self, session_factory, config, org):
        first = await _contribute(session_factory, config, org)
        second = await _contribute(
            session_factory, config, org,
            title="Deploy fails when the config file is missing",
            context_text="startup crashes with a FileNotFoundError during boot",
        )
        assert first["id"] in second["possible_duplicates"]

    async def test_an_unrelated_trace_is_not_flagged(self, session_factory, config, org):
        await _contribute(session_factory, config, org)
        unrelated = await _contribute(
            session_factory, config, org,
            title="zzqrx completely different subject entirely",
            context_text="wwvut nothing at all like the other trace",
            tags=["unrelated-domain"],
        )
        assert unrelated["possible_duplicates"] == []

    async def test_the_first_trace_in_an_org_has_nothing_to_duplicate(
        self, session_factory, config, org
    ):
        first = await _contribute(session_factory, config, org)
        assert first["possible_duplicates"] == []

    async def test_a_trace_with_no_tags_skips_the_check_entirely(
        self, session_factory, config, org
    ):
        await _contribute(session_factory, config, org)
        untagged = await _contribute(
            session_factory, config, org,
            title="Deploy fails when the config file is missing",
            context_text="startup crashes with a FileNotFoundError during boot",
            tags=[],
        )
        assert untagged["possible_duplicates"] == []

    async def test_is_informational_only_the_duplicate_stays_live(
        self, session_factory, config, org
    ):
        first = await _contribute(session_factory, config, org)
        second = await _contribute(
            session_factory, config, org,
            title="Deploy fails when the config file is missing",
            context_text="startup crashes with a FileNotFoundError during boot",
        )
        async with session_scope(session_factory) as session:
            fetched_first = await crud.get_trace(session, org, first["id"])
            fetched_second = await crud.get_trace(session, org, second["id"])
        assert fetched_first is not None and fetched_first["superseded_at"] == ""
        assert fetched_second is not None

    async def test_a_superseded_trace_is_not_offered_as_a_duplicate_candidate(
        self, session_factory, config, org
    ):
        original = await _contribute(session_factory, config, org)
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            await crud.amend_trace(
                session, org, original["id"], config, rate_limiter,
                title="v2 wording", actor="test",
            )
        third = await _contribute(
            session_factory, config, org,
            title="Deploy fails when the config file is missing",
            context_text="startup crashes with a FileNotFoundError during boot",
        )
        assert original["id"] not in third["possible_duplicates"]


class TestIdempotentReplayReturnsEmpty:
    async def test_a_replayed_contribute_returns_no_duplicates(
        self, session_factory, config, org
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            first = await crud.contribute_trace(
                session, org, config, rate_limiter, actor="test",
                title="t", context_text="c", solution_text="s",
                tags=["x"], idempotency_key="k1",
            )
        async with session_scope(session_factory) as session:
            replay = await crud.contribute_trace(
                session, org, config, rate_limiter, actor="test",
                title="t", context_text="c", solution_text="s",
                tags=["x"], idempotency_key="k1",
            )
        assert replay["id"] == first["id"]
        assert replay["possible_duplicates"] == []
