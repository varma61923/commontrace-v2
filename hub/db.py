"""Async SQLAlchemy engine/session plumbing."""

from __future__ import annotations

import logging
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from hub import auth
from hub.config import HubConfig

logger = logging.getLogger("commontrace.hub")
_SIGNERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def make_engine(config: HubConfig) -> AsyncEngine:
    connect_args = {}
    if config.db_statement_timeout_ms > 0:
        connect_args["server_settings"] = {
            "statement_timeout": str(config.db_statement_timeout_ms)
        }

    engine = create_async_engine(
        config.database_url,
        pool_pre_ping=True,
        connect_args=connect_args,
        pool_size=config.db_pool_size,
        max_overflow=config.db_max_overflow,
        pool_timeout=config.db_pool_timeout,
        pool_recycle=config.db_pool_recycle,
    )
    _SIGNERS[engine] = (config.ledger_signing_key, config.transparency_algorithm)
    return engine


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    key, algorithm = _SIGNERS.get(engine, ("", "hmac-sha256"))
    info = {"transparency_key": key, "transparency_algorithm": algorithm} if key else {}
    return async_sessionmaker(engine, expire_on_commit=False, info=info)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
    *, org_id: str | None = None,
) -> AsyncIterator[AsyncSession]:
    """Commit one transaction, optionally binding it to an authenticated org.

    Explicit bindings also scope nested sessions and are restored on every
    exit. Calls without an explicit org retain the request context (or the
    intentional unscoped context of operator/background work).
    """
    token = auth.current_org_id.set(org_id) if org_id is not None else None
    try:
        async with session_factory() as session:
            try:
                await _scope_to_current_org(session)
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
            pending = session.info.pop("capability_outbox", None)
        if pending:
            from hub.delegation import after_commit

            await after_commit(session_factory, *pending)
    finally:
        if token is not None:
            auth.current_org_id.reset(token)


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
_RLS_UNPROTECTED_SQL = text(
    "SELECT DISTINCT p.tablename FROM pg_policies p "
    "JOIN pg_namespace n ON n.nspname = p.schemaname "
    "JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = p.tablename "
    "WHERE p.schemaname = current_schema() AND (NOT c.relrowsecurity OR "
    "(NOT c.relforcerowsecurity AND pg_has_role(c.relowner, 'USAGE'))) ORDER BY p.tablename"
)


async def rls_status(session) -> dict:
    """Is row-level security actually protecting this connection?"""
    role = (await session.execute(text("SELECT current_user"))).scalar_one()
    bypasses = bool((await session.execute(_RLS_BYPASS_SQL)).scalar_one())
    policies = int((await session.execute(_RLS_POLICIES_SQL)).scalar_one())
    unprotected = list((await session.execute(_RLS_UNPROTECTED_SQL)).scalars())
    return {
        "role": role,
        "bypasses_rls": bypasses,
        "policy_count": policies,
        "unprotected_tables": unprotected,
        "enforced": policies > 0 and not bypasses and not unprotected,
    }


class RowLevelSecurityError(RuntimeError):
    ...


async def check_row_level_security(
    session_factory, *, allow_bypass: bool = True, require: bool = False
) -> dict | None:
    try:
        async with session_factory() as session:
            status = await rls_status(session)
    except Exception as exc:  # noqa: BLE001 - optional diagnostics may degrade; required checks fail closed
        if require:
            raise RowLevelSecurityError(
                "refusing to start: HUB_REQUIRE_RLS is set, but database row-level security "
                "could not be verified. Check database connectivity, migrations and runtime-role permissions."
            ) from None
        logger.warning("could not determine row-level security status", extra={"error_type": type(exc).__name__})
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
            "hub/postgres-init/10-runtime-role.sql). An intentional bypass requires "
            "HUB_ALLOW_RLS_BYPASS=true and HUB_REQUIRE_RLS=false."
        )
        if not allow_bypass or require:
            requirement = "HUB_REQUIRE_RLS is set; " if require else ""
            raise RowLevelSecurityError(f"refusing to start: {requirement}{message}")
        logger.warning("%s", message)
    elif status["unprotected_tables"] and (not allow_bypass or require):
        raise RowLevelSecurityError(
            "refusing to start: row-level security policies are installed but disabled or bypassed by "
            "table ownership on: " + ", ".join(status["unprotected_tables"]) + ". "
            "Enable row-level security and FORCE it for owner roles, or acknowledge an intentional "
            "bypass with HUB_ALLOW_RLS_BYPASS=true and HUB_REQUIRE_RLS=false."
        )
    elif require and not status["enforced"]:
        raise RowLevelSecurityError(
            "refusing to start: HUB_REQUIRE_RLS is set, but row-level security is not "
            f"enforced for role {status['role']!r} ({status['policy_count']} policies "
            "installed). Run `alembic -c hub/alembic.ini upgrade head` so the policies "
            "exist, and connect as a role that cannot bypass them."
        )
    elif status["unprotected_tables"]:
        logger.warning("row-level security is not enforced on policy-backed tables: %s",
                       ", ".join(status["unprotected_tables"]))
    elif status["enforced"]:
        logger.info(
            "row-level security active: %d policies enforced for role %r",
            status["policy_count"], status["role"],
        )
    return status
