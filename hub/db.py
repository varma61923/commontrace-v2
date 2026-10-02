"""Async SQLAlchemy engine/session plumbing."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from hub import auth
from hub.config import HubConfig

logger = logging.getLogger("commontrace.hub")


def make_engine(config: HubConfig) -> AsyncEngine:
    connect_args = {}
    if config.db_statement_timeout_ms > 0:
        connect_args["server_settings"] = {
            "statement_timeout": str(config.db_statement_timeout_ms)
        }

    return create_async_engine(
        config.database_url,
        pool_pre_ping=True,
        connect_args=connect_args,
        pool_size=config.db_pool_size,
        max_overflow=config.db_max_overflow,
        pool_timeout=config.db_pool_timeout,
        pool_recycle=config.db_pool_recycle,
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        try:
            await _scope_to_current_org(session)
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def _scope_to_current_org(session: AsyncSession) -> None:
    org_id = auth.current_org_id.get(None)
    if not org_id:
        return
    await session.execute(
        text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org_id)}
    )

_RLS_BYPASS_SQL = text(
    "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
)
_RLS_POLICIES_SQL = text(
    "SELECT count(*) FROM pg_policies WHERE schemaname = current_schema()"
)


async def rls_status(session) -> dict:
    """Is row-level security actually protecting this connection?"""
    role = (await session.execute(text("SELECT current_user"))).scalar_one()
    bypasses = bool((await session.execute(_RLS_BYPASS_SQL)).scalar_one())
    policies = int((await session.execute(_RLS_POLICIES_SQL)).scalar_one())
    return {
        "role": role,
        "bypasses_rls": bypasses,
        "policy_count": policies,
        "enforced": policies > 0 and not bypasses,
    }


class RowLevelSecurityError(RuntimeError):
    ...


async def check_row_level_security(
    session_factory, *, allow_bypass: bool = True, require: bool = False
) -> dict | None:
    try:
        async with session_factory() as session:
            status = await rls_status(session)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never break startup
        logger.warning("could not determine row-level security status: %r", exc)
        return None

    inert = bool(status["policy_count"]) and bool(status["bypasses_rls"])
    if inert:
        message = (
            f"row-level security is INSTALLED BUT INERT: {status['policy_count']} "
            f"policies exist, but the connecting role {status['role']!r} is a "
            "superuser or has BYPASSRLS, and Postgres skips every policy for such a "
            "role -- silently. Tenant isolation would rest entirely on hub/crud.py's "
            "own org_id predicates, with no database-enforced backstop behind them. "
            "Point HUB_DATABASE_URL at a non-superuser role that owns nothing and "
            "has no BYPASSRLS (docker-compose.yml ships one: see "
            "hub/postgres-init/10-runtime-role.sql), or set "
            "HUB_ALLOW_RLS_BYPASS=true to acknowledge this is intentional."
        )
        if not allow_bypass:
            raise RowLevelSecurityError(f"refusing to start: {message}")
        logger.warning("%s", message)
    elif require and not status["enforced"]:
        raise RowLevelSecurityError(
            "refusing to start: HUB_REQUIRE_RLS is set, but row-level security is not "
            f"enforced for role {status['role']!r} ({status['policy_count']} policies "
            "installed). Run `alembic -c hub/alembic.ini upgrade head` so the policies "
            "exist, and connect as a role that cannot bypass them."
        )
    elif status["enforced"]:
        logger.info(
            "row-level security active: %d policies enforced for role %r",
            status["policy_count"], status["role"],
        )
    return status
