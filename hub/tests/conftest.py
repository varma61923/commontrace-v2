from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text

from hub.config import HubConfig
from hub.db import make_engine, make_session_factory
from hub.models import Base

TEST_DATABASE_URL = os.environ.get(
    "HUB_TEST_DATABASE_URL",
    "postgresql+asyncpg://commontrace_dev:devpassword@localhost:5432/commontrace_hub_test",
)


def _skip_if_no_db():
    if not TEST_DATABASE_URL:
        pytest.skip("HUB_TEST_DATABASE_URL not set; Hub tests need a real Postgres instance")


@pytest_asyncio.fixture
async def config() -> HubConfig:
    _skip_if_no_db()
    return HubConfig(database_url=TEST_DATABASE_URL, rate_limit_per_minute=1_000_000, rate_limit_burst=1_000_000)


@pytest_asyncio.fixture(scope="session")
async def _schema():
    """Drop and recreate the whole schema once per test session.

    `create_all` alone is not enough: it creates *missing* tables but never
    ALTERs existing ones, so a test database left over from an older revision
    silently keeps its old columns and every test fails on the first new one.
    Dropping first makes the test schema always match hub/models.py exactly,
    independent of whatever state the database was left in.

    That does mean this suite validates the *models*, not the migrations.
    Migrations are verified separately by running `alembic upgrade head` in
    CI (see .github/workflows/ci.yml) -- so a model change that nobody wrote
    a migration for still gets caught, just by a different check.
    """
    _skip_if_no_db()
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
            # Truncate between tests: cheap, and keeps the schema in place
            # for the next test without re-running DDL.
            for table in reversed(Base.metadata.sorted_tables):
                await conn.execute(text(f'TRUNCATE TABLE "{table.name}" CASCADE'))
        await engine.dispose()
