from __future__ import annotations

import pytest
import pytest_asyncio

from hub import crud
from hub.abuse import make_rate_limiter
from hub.crud import IdempotencyKeyConflict, TraceRejected
from hub.db import session_scope
from hub.models import Organization

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="profile-input-org")
        session.add(o)
        await session.flush()
        return o.id


class TestContributeTraceProfile:
    async def test_a_profile_is_stored_and_read_back(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            result = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[],
                agent_type="code", actor="test", profile="code-review",
            )
        async with session_scope(session_factory) as session:
            fetched = await crud.get_trace(session, org, result["id"])
        assert fetched["profile"] == "code-review"

    async def test_omitted_profile_stores_the_empty_default(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            result = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[],
                agent_type="code", actor="test",
            )
        async with session_scope(session_factory) as session:
            fetched = await crud.get_trace(session, org, result["id"])
        assert fetched["profile"] == ""

    async def test_an_overlong_profile_is_rejected_rather_than_truncated_by_postgres(
        self, session_factory, org, config
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(TraceRejected, match="profile"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s", tags=[],
                    agent_type="code", actor="test", profile="x" * 129,
                )

    async def test_amend_trace_carries_the_profile_forward_unchanged(
        self, session_factory, org, config
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            original = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[],
                agent_type="code", actor="test", profile="code-review",
            )
        async with session_scope(session_factory) as session:
            amended = await crud.amend_trace(
                session, org, original["id"], config, rate_limiter,
                title="t2", actor="test",
            )
        async with session_scope(session_factory) as session:
            fetched = await crud.get_trace(session, org, amended["id"])
        assert fetched["profile"] == "code-review"

    async def test_idempotent_retry_with_the_same_profile_returns_the_original(
        self, session_factory, org, config
    ):
        rate_limiter = make_rate_limiter(config)
        key = "profile-retry-key-1"
        async with session_scope(session_factory) as session:
            first = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[],
                agent_type="code", actor="test", idempotency_key=key, profile="code-review",
            )
        async with session_scope(session_factory) as session:
            retry = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[],
                agent_type="code", actor="test", idempotency_key=key, profile="code-review",
            )
        assert retry["id"] == first["id"]

    async def test_reusing_a_key_with_a_different_profile_conflicts(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        key = "profile-retry-key-2"
        async with session_scope(session_factory) as session:
            await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[],
                agent_type="code", actor="test", idempotency_key=key, profile="code-review",
            )
        async with session_scope(session_factory) as session:
            with pytest.raises(IdempotencyKeyConflict):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s", tags=[],
                    agent_type="code", actor="test", idempotency_key=key, profile="other-profile",
                )

    async def test_a_key_first_used_with_no_profile_still_replays_cleanly(
        self, session_factory, org, config
    ):
        rate_limiter = make_rate_limiter(config)
        key = "profile-retry-key-3"
        async with session_scope(session_factory) as session:
            first = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[],
                agent_type="code", actor="test", idempotency_key=key,
            )
        async with session_scope(session_factory) as session:
            retry = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[],
                agent_type="code", actor="test", idempotency_key=key,
            )
        assert retry["id"] == first["id"]
