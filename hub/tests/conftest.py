from __future__ import annotations

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

# This default matches the Postgres role/db this project's own dev sandbox
# provisions out of the box, so hub tests "just work" there with no extra
# setup -- CI and hub/README.md's documented local workflow both set
# HUB_TEST_DATABASE_URL explicitly and override it.
TEST_DATABASE_URL = os.environ.get(
    "HUB_TEST_DATABASE_URL",
    "postgresql+asyncpg://commontrace_dev:devpassword@localhost:5432/commontrace_hub_test",
)

# Memoized across the whole test session: _skip_if_no_db() runs on every
# test that needs the `config`/`_schema` fixtures (hundreds of them), and a
# real connection attempt each time would add real per-test latency for no
# benefit once the first attempt has already answered the question.
_db_reachable: bool | None = None


async def _skip_if_no_db() -> None:
    """Skip cleanly when Postgres is not actually reachable at
    TEST_DATABASE_URL -- not just when the env var happens to be unset.

    A bare `if not TEST_DATABASE_URL` can never be true: the module-level
    default above is a non-empty string, so this promised-but-never-taken
    skip path meant any environment without a Postgres matching that exact
    hardcoded default (a plain contributor laptop, a lightweight sandbox
    without this project's specific dev provisioning) failed every hub
    test with a raw ConnectionRefusedError deep in fixture setup instead of
    the clean, actionable skip this function's own name promises. A real
    (short-timeout) connection attempt is what actually answers "is there a
    db" -- the string being non-empty never did.
    """
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
            # Truncate between tests: cheap, and keeps the schema in place
            # for the next test without re-running DDL.
            for table in reversed(Base.metadata.sorted_tables):
                await conn.execute(text(f'TRUNCATE TABLE "{table.name}" CASCADE'))
        await engine.dispose()


@pytest.fixture
def establish_orgs(session_factory):
    """Factory: promote existing organizations to "established" voters.

    hub/crud.py:vote_trace only tallies votes from orgs that clear the
    autoconfirmed bar in hub/commons.py -- old enough, with real traces
    behind them. A freshly inserted `Organization()` clears neither, so any
    test about what voting *does* (standing, ranking, coverage claims,
    review queues) would silently be measuring the anti-sockpuppet rule
    instead of the thing it is about. Those tests call this on their voter
    orgs; the tests that are about the rule itself deliberately do not.

    `trace_count` is set directly rather than by contributing N traces:
    it is the maintained counter the policy actually reads, and
    hub/tests/test_trace_count.py is what keeps that counter honest against
    a real `count(*)` -- inserting five throwaway traces per voter here
    would cost every one of these tests real time and prove nothing extra.
    """

    async def _establish(*org_ids) -> None:
        # Accepts ids loose or in one iterable, so both `establish_orgs(a, b)`
        # and `establish_orgs(list_of_ids)` read naturally at the call site.
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
