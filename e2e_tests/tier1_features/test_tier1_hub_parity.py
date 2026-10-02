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
async def hub_session():
    engine = create_async_engine(TEST_DATABASE_URL, connect_args={"timeout": 3})
    try:
        async with engine.connect():
            pass
    except Exception:
        pytest.skip("Postgres Hub test database is not reachable")

    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        org = Organization(name="e2e-hub-test-org")
        session.add(org)
        await session.flush()
        yield session, org.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_t1_hub_contribute_with_scopes_and_validity(hub_session):
    """E2E-T1-HUB-1: Contribute trace with scoped routing and bitemporal validity bounds."""
    session, org_id = hub_session
    config = HubConfig(database_url=TEST_DATABASE_URL)
    limiter = RateLimiter(per_minute=1000, burst=1000)

    res = await crud.contribute_trace(
        session, org_id, config, limiter,
        title="Payment webhook retry policy",
        context_text="Handling 500 errors from stripe webhooks",
        solution_text="Retry with exponential backoff up to 5 attempts",
        tags=["payment", "stripe"],
        scopes=["payments", "billing"],
        valid_from="2026-01-01T00:00:00Z",
        valid_until="2027-01-01T00:00:00Z",
    )
    assert res["id"] is not None
    assert not res["quarantined"]


@pytest.mark.asyncio
async def test_t1_hub_scoped_search_containment(hub_session):
    """E2E-T1-HUB-2: Hub search by scope matches explicitly scoped and global traces, excluding others."""
    session, org_id = hub_session
    config = HubConfig(database_url=TEST_DATABASE_URL)
    limiter = RateLimiter(per_minute=1000, burst=1000)

    # Scoped trace: infra
    t_infra = await crud.contribute_trace(
        session, org_id, config, limiter,
        title="Infra Terraform state lock",
        context_text="Terraform lock expired in S3",
        solution_text="Force release DynamoDB lock",
        tags=["infra"],
        scopes=["infra"],
    )

    # Scoped trace: frontend
    t_front = await crud.contribute_trace(
        session, org_id, config, limiter,
        title="Frontend Vite bundling error",
        context_text="Vite build chunk size exceeded",
        solution_text="Split chunks in vite.config.ts",
        tags=["frontend"],
        scopes=["frontend"],
    )

    # Global trace
    t_global = await crud.contribute_trace(
        session, org_id, config, limiter,
        title="Global git pre-commit hook",
        context_text="Format staged files before commit",
        solution_text="Run prettier and black",
        tags=["git"],
        scopes=[],
    )

    # Search with scope='infra'
    found_infra = await crud.search_traces(session, org_id, scope="infra")
    found_ids = {t["id"] for t in found_infra["traces"]}
    assert t_infra["id"] in found_ids, "Infra-scoped trace must be included"
    assert t_global["id"] in found_ids, "Global trace must be included"
    assert t_front["id"] not in found_ids, "Frontend trace must be excluded from infra scope"


@pytest.mark.asyncio
async def test_t1_hub_bitemporal_as_of_filtering(hub_session):
    """E2E-T1-HUB-3: Hub search with as_of point-in-time filtering matches valid time window."""
    session, org_id = hub_session
    config = HubConfig(database_url=TEST_DATABASE_URL)
    limiter = RateLimiter(per_minute=1000, burst=1000)

    # 2025 trace
    t_2025 = await crud.contribute_trace(
        session, org_id, config, limiter,
        title="2025 Python 3.9 migration",
        context_text="Upgrading services to Python 3.9",
        solution_text="Update Dockerfile base image",
        tags=["python"],
        valid_from="2025-01-01T00:00:00Z",
        valid_until="2025-12-31T23:59:59Z",
    )

    # 2026 trace
    t_2026 = await crud.contribute_trace(
        session, org_id, config, limiter,
        title="2026 Python 3.12 migration",
        context_text="Upgrading services to Python 3.12",
        solution_text="Use modern syntax and pyproject.toml",
        tags=["python"],
        valid_from="2026-01-01T00:00:00Z",
        valid_until="2026-12-31T23:59:59Z",
    )

    # Query as of mid-2025
    found_2025 = await crud.search_traces(session, org_id, as_of="2025-06-01T00:00:00Z")
    ids_2025 = {t["id"] for t in found_2025["traces"]}
    assert t_2025["id"] in ids_2025
    assert t_2026["id"] not in ids_2025

    # Query as of mid-2026
    found_2026 = await crud.search_traces(session, org_id, as_of="2026-06-01T00:00:00Z")
    ids_2026 = {t["id"] for t in found_2026["traces"]}
    assert t_2026["id"] in ids_2026
    assert t_2025["id"] not in ids_2026


def test_t1_hub_doctor_health_checks(isolated_store: str):
    """E2E-T1-HUB-4: Diagnostics command commontrace doctor passes all environment and store health checks."""
    res_doc = run_cli("doctor", dest=isolated_store)
    res_doc.assert_success()
    assert "ok" in res_doc.stdout.lower() or "check" in res_doc.stdout.lower()


def test_t1_hub_alembic_migration_exists():
    """E2E-T1-HUB-5: Verify Alembic migrations directory contains required migrations."""
    alembic_dir = "/root/Test/commontrace-v2/hub/alembic/versions"
    assert os.path.exists(alembic_dir), "Alembic versions directory must exist"
    migration_files = [f for f in os.listdir(alembic_dir) if f.endswith(".py")]
    assert len(migration_files) >= 35, "Must have comprehensive Alembic migrations in hub"
