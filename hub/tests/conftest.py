from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import create_async_engine

from hub import commons
from hub.config import HubConfig
from hub.db import make_engine, make_session_factory, session_scope
from hub.models import Base, Organization


def _per_worker_database() -> None:
    """Under pytest-xdist each worker gets its own database (`<name>_gw0`, ...), created
    on first use, so parallel workers never truncate each other's tables. Runs at import,
    before any test module reads HUB_TEST_DATABASE_URL; without xdist nothing changes."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    url = os.environ.get("HUB_TEST_DATABASE_URL", "")
    if not worker or not url or not worker.isalnum() or os.environ.get("HUB_TEST_DATABASE_WORKER") == worker:
        return
    base, sep, query = url.partition("?")
    head, _, name = base.rpartition("/")
    target = f"{name}_{worker}"
    os.environ["HUB_TEST_DATABASE_URL"] = f"{head}/{target}{sep}{query}"
    os.environ["HUB_TEST_DATABASE_WORKER"] = worker

    async def create() -> None:
        engine = create_async_engine(url, isolation_level="AUTOCOMMIT", connect_args={"timeout": 3})
        try:
            async with engine.connect() as conn:
                exists = await conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"),
                                           {"name": target})
                if not exists:
                    await conn.execute(text(f'CREATE DATABASE "{target}"'))
        finally:
            await engine.dispose()

    try:
        asyncio.run(create())
    except Exception:  # noqa: BLE001 - an unreachable server skips the tests as before
        pass


_per_worker_database()

TEST_DATABASE_URL = os.environ.get(
    "HUB_TEST_DATABASE_URL",
    "postgresql+asyncpg://commontrace_dev:devpassword@localhost:5432/commontrace_hub_test",
)

_db_reachable: bool | None = None


async def _skip_if_no_db() -> None:
    global _db_reachable
    if not TEST_DATABASE_URL:
        pytest.skip("HUB_TEST_DATABASE_URL not set; Hub tests need a real Postgres instance")
    if _db_reachable is None:
        probe_engine = create_async_engine(TEST_DATABASE_URL, connect_args={"timeout": 3})
        try:
            async with probe_engine.connect():
                pass
            _db_reachable = True
        except Exception:  # noqa: BLE001 - any failure means "not reachable", not a crash here
            _db_reachable = False
        finally:
            await probe_engine.dispose()
    if not _db_reachable:
        pytest.skip(
            f"Cannot reach Postgres at the configured HUB_TEST_DATABASE_URL "
            f"({TEST_DATABASE_URL!r}); Hub tests need a real, reachable Postgres instance."
        )


@pytest_asyncio.fixture
async def config() -> HubConfig:
    await _skip_if_no_db()
    return HubConfig(database_url=TEST_DATABASE_URL, rate_limit_per_minute=1_000_000, rate_limit_burst=1_000_000)


@pytest_asyncio.fixture(scope="session")
async def _schema():
    await _skip_if_no_db()
    engine = make_engine(HubConfig(database_url=TEST_DATABASE_URL))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(config: HubConfig, _schema):
    engine = make_engine(config)
    try:
        yield make_session_factory(engine)
    finally:
        async with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                await conn.execute(text(f'TRUNCATE TABLE "{table.name}" CASCADE'))
        await engine.dispose()


@pytest.fixture
def establish_orgs(session_factory):
    async def _establish(*org_ids) -> None:
        ids = [
            oid
            for group in org_ids
            for oid in ([group] if isinstance(group, str) else group)
        ]
        backdated = datetime.now(timezone.utc) - timedelta(
            hours=commons.COMMONS_VOTER_MIN_AGE_HOURS + 1
        )
        async with session_scope(session_factory) as session:
            await session.execute(
                update(Organization)
                .where(Organization.id.in_(ids))
                .values(
                    trace_count=commons.COMMONS_VOTER_MIN_TRACES,
                    created_at=backdated,
                )
            )

    return _establish
