from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

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
async def hub_governance_session():
    engine = create_async_engine(TEST_DATABASE_URL, connect_args={"timeout": 3})
    try:
        async with engine.connect():
            pass
    except Exception:
        pytest.skip("Postgres Hub test database is not reachable")

    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        org = Organization(name="enterprise-governance-org")
        session.add(org)
        await session.flush()
        yield session, org.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_t4_multi_tenant_scoped_governance(hub_governance_session, isolated_store: str):
    """E2E-T4-RW-3: Enterprise multi-tenant deployment verifies strict scope isolation
    across backend, frontend, data-infra.
    """
    session, org_id = hub_governance_session
    config = HubConfig(database_url=TEST_DATABASE_URL)
    limiter = RateLimiter(per_minute=1000, burst=1000)

    # 1. Global policy trace: All teams must follow TLS 1.3
    t_global = await crud.contribute_trace(
        session, org_id, config, limiter,
        title="Enterprise Security Policy TLS 1.3",
        context_text="Mandatory corporate cryptographic cipher suite requirement",
        solution_text="Enforce TLS 1.3 on all internal and external communication endpoints",
        tags=["security", "compliance"],
        scopes=[],
    )

    # 2. Backend scoped trace: Connection pooling
    t_backend = await crud.contribute_trace(
        session, org_id, config, limiter,
        title="Backend PgBouncer connection pooling",
        context_text="Postgres connection starvation prevention",
        solution_text="All database clients connect through PgBouncer",
        tags=["database", "backend"],
        scopes=["backend"],
    )

    # 3. Frontend scoped trace: Next.js bundle chunking
    t_frontend = await crud.contribute_trace(
        session, org_id, config, limiter,
        title="Frontend Webpack code-splitting policy",
        context_text="Initial load performance optimization",
        solution_text="Dynamic import for heavy charting components",
        tags=["frontend", "performance"],
        scopes=["frontend"],
    )

    # 4. Data-infra scoped trace: Spark shuffle partitions
    t_data = await crud.contribute_trace(
        session, org_id, config, limiter,
        title="Data infra Spark shuffle tuning",
        context_text="Out of memory errors during nightly ETL",
        solution_text="Set spark.sql.shuffle.partitions=800",
        tags=["spark", "etl"],
        scopes=["data-infra"],
    )

    # Verify Backend query: Sees global + backend, but NOT frontend or data-infra
    found_backend = await crud.search_traces(session, org_id, scope="backend")
    backend_ids = {t["id"] for t in found_backend["traces"]}
    assert t_global["id"] in backend_ids, "Backend must see global security policy"
    assert t_backend["id"] in backend_ids, "Backend must see backend traces"
    assert t_frontend["id"] not in backend_ids, "Backend must NOT see frontend traces"
    assert t_data["id"] not in backend_ids, "Backend must NOT see data-infra traces"

    # Verify Frontend query: Sees global + frontend, but NOT backend or data-infra
    found_frontend = await crud.search_traces(session, org_id, scope="frontend")
    frontend_ids = {t["id"] for t in found_frontend["traces"]}
    assert t_global["id"] in frontend_ids
    assert t_frontend["id"] in frontend_ids
    assert t_backend["id"] not in frontend_ids
    assert t_data["id"] not in frontend_ids

    # Verify Data-infra query: Sees global + data-infra, but NOT backend or frontend
    found_data = await crud.search_traces(session, org_id, scope="data-infra")
    data_ids = {t["id"] for t in found_data["traces"]}
    assert t_global["id"] in data_ids
    assert t_data["id"] in data_ids
    assert t_backend["id"] not in data_ids
    assert t_frontend["id"] not in data_ids
