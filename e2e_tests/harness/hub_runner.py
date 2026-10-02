from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "HUB_TEST_DATABASE_URL",
    "postgresql+asyncpg://commontrace_dev:devpassword@localhost:5432/commontrace_hub_test",
)


class HubTestHelper:
    @staticmethod
    async def is_db_available() -> bool:
        if not TEST_DATABASE_URL:
            return False
        probe_engine = create_async_engine(TEST_DATABASE_URL, connect_args={"timeout": 3})
        try:
            async with probe_engine.connect():
                return True
        except Exception:
            return False
        finally:
            await probe_engine.dispose()
