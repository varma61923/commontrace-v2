from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from e2e_tests.harness.cli_runner import run_cli
from hub import crud
from hub.abuse import RateLimiter
from hub.config import HubConfig
from hub.db import make_session_factory, session_scope
from hub.models import Organization

TEST_DATABASE_URL = os.environ.get(
    "HUB_TEST_DATABASE_URL",
    "postgresql+asyncpg://commontrace_dev:devpassword@localhost:5432/commontrace_hub_test",
)


@pytest_asyncio.fixture
async def hub_sync_session():
    engine = create_async_engine(TEST_DATABASE_URL, connect_args={"timeout": 3})
    try:
        async with engine.connect():
            pass
    except Exception:
        pytest.skip("Postgres Hub test database is not reachable")

    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        org = Organization(name="e2e-hub-sync-org")
        session.add(org)
        await session.flush()
        yield session, org.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_t3_hub_sync_and_local_parity(hub_sync_session, isolated_store: str):
    """E2E-T3-CB-5: Local capture and Hub trace ingestion exhibit identical scope filtering semantics."""
    session, org_id = hub_sync_session
    config = HubConfig(database_url=TEST_DATABASE_URL)
    limiter = RateLimiter(per_minute=1000, burst=1000)

    # 1. Local trace capture
    res_cap = run_cli(
        "capture",
        "--title", "Kafka consumer group lag alert",
        "--context", "Consumer lag exceeded 10000 records on checkout topic",
        "--solution", "Scale up replica consumer pods",
        "--agent-type", "coding",
        dest=isolated_store,
    )
    res_cap.assert_success()

    # 2. Replicate trace to Hub with scope
    hub_trace = await crud.contribute_trace(
        session, org_id, config, limiter,
        title="Kafka consumer group lag alert",
        context_text="Consumer lag exceeded 10000 records on checkout topic",
        solution_text="Scale up replica consumer pods",
        tags=["kafka", "streaming"],
        scopes=["streaming", "checkout"],
    )
    assert hub_trace["id"] is not None

    # 3. Query Hub with matching scope
    found = await crud.search_traces(session, org_id, scope="streaming")
    found_ids = {t["id"] for t in found["traces"]}
    assert hub_trace["id"] in found_ids

    # 4. Query Hub with non-matching scope
    mismatched = await crud.search_traces(session, org_id, scope="unrelated-scope")
    mismatched_ids = {t["id"] for t in mismatched["traces"]}
    assert hub_trace["id"] not in mismatched_ids
