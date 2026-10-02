from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from hub import crud
from hub.abuse import RateLimited, RateLimiter
from hub.config import HubConfig
from hub.db import make_session_factory, session_scope
from hub.models import Organization

TEST_DATABASE_URL = os.environ.get(
    "HUB_TEST_DATABASE_URL",
    "postgresql+asyncpg://commontrace_dev:devpassword@localhost:5432/commontrace_hub_test",
)


@pytest_asyncio.fixture
async def hub_boundary_session():
    engine = create_async_engine(TEST_DATABASE_URL, connect_args={"timeout": 3})
    try:
        async with engine.connect():
            pass
    except Exception:
        pytest.skip("Postgres Hub test database is not reachable")

    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        org = Organization(name="e2e-hub-boundary-org")
        session.add(org)
        await session.flush()
        yield session, org.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_t2_hub_idempotency_key_replay(hub_boundary_session):
    """E2E-T2-HUB-1: Submitting identical trace with same idempotency key returns original trace ID."""
    session, org_id = hub_boundary_session
    config = HubConfig(database_url=TEST_DATABASE_URL)
    limiter = RateLimiter(per_minute=1000, burst=1000)

    key = "idem-key-12345"
    t1 = await crud.contribute_trace(
        session, org_id, config, limiter,
        title="Idempotent trace test",
        context_text="Ctx",
        solution_text="Sol",
        idempotency_key=key,
    )

    t2 = await crud.contribute_trace(
        session, org_id, config, limiter,
        title="Idempotent trace test",
        context_text="Ctx",
        solution_text="Sol",
        idempotency_key=key,
    )

    assert t1["id"] == t2["id"], "Idempotent re-submission must return the exact same trace ID"


@pytest.mark.asyncio
async def test_t2_hub_unmatched_search_empty_result(hub_boundary_session):
    """E2E-T2-HUB-2: Searching for completely unmatched query terms returns empty list without error."""
    session, org_id = hub_boundary_session
    found = await crud.search_traces(session, org_id, query="nonexistentxyzzy987654321")
    assert found["traces"] == []
    assert found["has_more"] is False


@pytest.mark.asyncio
async def test_t2_hub_rate_limiting_enforcement(hub_boundary_session):
    """E2E-T2-HUB-3: Rate limiter with 0 allowance immediately raises RateLimited."""
    session, org_id = hub_boundary_session
    config = HubConfig(database_url=TEST_DATABASE_URL)
    strict_limiter = RateLimiter(per_minute=1, burst=1)

    # First request consumes token
    await crud.contribute_trace(
        session, org_id, config, strict_limiter,
        title="Rate limit test 1",
        context_text="Ctx",
        solution_text="Sol",
    )

    # Second immediate request should hit rate limit
    with pytest.raises(RateLimited):
        await crud.contribute_trace(
            session, org_id, config, strict_limiter,
            title="Rate limit test 2",
            context_text="Ctx",
            solution_text="Sol",
        )


@pytest.mark.asyncio
async def test_t2_hub_invalid_as_of_format_graceful(hub_boundary_session):
    """E2E-T2-HUB-4: Malformed as_of timestamp does not crash the search query."""
    session, org_id = hub_boundary_session
    # Pass completely invalid timestamp string
    found = await crud.search_traces(session, org_id, as_of="invalid-date-format-abc")
    assert "traces" in found


@pytest.mark.asyncio
async def test_t2_hub_nonexistent_trace_lookup(hub_boundary_session):
    """E2E-T2-HUB-5: Reading a nonexistent trace ID returns None cleanly."""
    session, org_id = hub_boundary_session
    trace = await crud.get_trace(session, org_id, "00000000-0000-0000-0000-000000000000")
    assert trace is None
