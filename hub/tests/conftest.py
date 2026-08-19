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


@pytest_asyncio.fixture
async def session_factory(config: HubConfig):
    engine = make_engine(config)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield make_session_factory(engine)
    finally:
        async with engine.begin() as conn:
            # Truncate rather than drop: cheap, and keeps the schema in
            # place for the next test module without re-running DDL.
            for table in reversed(Base.metadata.sorted_tables):
                await conn.execute(text(f'TRUNCATE TABLE "{table.name}" CASCADE'))
        await engine.dispose()
