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
    """Explicit pool sizing rather than SQLAlchemy's defaults (pool_size=5,
    max_overflow=10). The ceiling that matters is Postgres's own
    max_connections, shared across every replica: pool_size * replicas must
    stay under it, which is a deployment-specific number and therefore
    configurable rather than hardcoded (see hub/.env.example).

    pool_recycle guards against a connection being closed underneath us by
    an idle timeout on a managed Postgres or a proxy in between; pool_pre_ping
    catches the case where that happened anyway.
    """
    connect_args = {}
    if config.db_statement_timeout_ms > 0:
        # Applied by Postgres itself, per connection, so it still bounds a
        # query whose caller has already timed out and walked away -- the
        # case no application-side timeout can reach, and the one that
        # otherwise leaves a runaway query holding a pooled connection
        # nobody is waiting for. asyncpg passes server_settings straight
        # through as connection parameters.
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
    """One transaction per request: commit on success, roll back on any
    exception so a half-applied write never lands (e.g. a trace insert that
    raises mid-way through an abuse-control check).

    Catches BaseException, not Exception: asyncio.CancelledError subclasses
    BaseException (not Exception, since Python 3.8), so a request cancelled
    mid-transaction -- a client disconnect, a server shutdown, a timeout
    cancelling the handling task -- would skip this rollback entirely under
    an `except Exception` and leave the session's pending writes uncommitted
    on the connection when it's returned to the pool instead of explicitly
    rolled back here.
    """
    async with session_factory() as session:
        try:
            await _scope_to_current_org(session)
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def _scope_to_current_org(session: AsyncSession) -> None:
    """Tell Postgres which org this transaction belongs to, for RLS.

    The org comes from the `auth.current_org_id` contextvar the auth
    middleware already sets per request, so no call site has to pass it and
    no path can forget to -- which is the point: a rule every caller must
    remember is the rule this exists to stop relying on.

    `set_config(..., true)` is the transaction-local form, i.e. the
    parameterised equivalent of SET LOCAL. It is used rather than
    f-string-building a `SET LOCAL app.org_id = '<id>'` statement because
    SET LOCAL cannot take a bind parameter, and interpolating a
    request-derived value into SQL is a injection sink -- a documented
    footgun of exactly this pattern in other projects that adopted it.

    Unset (operator CLI, benchmarks, alembic, anything outside a request)
    leaves the setting empty, which the policies read as "unscoped" -- the
    behaviour those paths had before RLS existed. See
    hub/alembic/versions/d5c8b3a91e77_row_level_security.py.
    """
    org_id = auth.current_org_id.get(None)
    if not org_id:
        return
    await session.execute(
        text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org_id)}
    )

# Roles that Postgres exempts from row-level security. A superuser is
# exempt always; BYPASSRLS is the explicit grant of the same exemption.
_RLS_BYPASS_SQL = text(
    "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
)
_RLS_POLICIES_SQL = text(
    "SELECT count(*) FROM pg_policies WHERE schemaname = current_schema()"
)


async def rls_status(session) -> dict:
    """Is row-level security actually protecting this connection?

    RLS has a failure mode that is worse than not having it: for a
    superuser (or any role with BYPASSRLS) Postgres skips every policy
    SILENTLY. No error, no warning, no log line -- the policies are
    present, `pg_policies` lists them, an audit finds them, and they do
    nothing. An operator reading the migration would reasonably conclude
    tenant isolation is enforced in the database when it is not.

    This is not a hypothetical shape. The Postgres image's POSTGRES_USER
    becomes the cluster superuser, and this project's own
    docker-compose.yml pointed HUB_DATABASE_URL at exactly that role --
    so the shipped default deployment installed the policies and bypassed
    them. CI caught it only because the RLS tests assert enforcement
    rather than existence; every other test passed either way.

    Returns the three facts a caller needs to say something useful:
    whether policies exist, whether this role bypasses them, and the role
    name to put in the message.
    """
    role = (await session.execute(text("SELECT current_user"))).scalar_one()
    bypasses = bool((await session.execute(_RLS_BYPASS_SQL)).scalar_one())
    policies = int((await session.execute(_RLS_POLICIES_SQL)).scalar_one())
    return {
        "role": role,
        "bypasses_rls": bypasses,
        "policy_count": policies,
        "enforced": policies > 0 and not bypasses,
    }


async def warn_if_rls_is_inert(session_factory) -> dict | None:
    """Log loudly when the policies are installed but cannot bite.

    Deliberately a warning rather than a refusal to start: an operator who
    runs the app as owner-superuser today would otherwise have a working
    deployment turned into a crash-loop by a migration, and RLS here is
    defence-in-depth BEHIND hub/crud.py's own org_id predicates -- those
    still work. What must not happen is believing in a guarantee that is
    not there, so the message says exactly what to do about it.
    """
    try:
        async with session_factory() as session:
            status = await rls_status(session)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never break startup
        logger.warning("could not determine row-level security status: %r", exc)
        return None

    if status["policy_count"] and status["bypasses_rls"]:
        logger.warning(
            "row-level security is INSTALLED BUT INERT: %d policies exist, but the "
            "connecting role %r is a superuser or has BYPASSRLS, and Postgres skips "
            "every policy for such a role -- silently. Tenant isolation currently "
            "rests entirely on hub/crud.py's own org_id predicates. Connect as a "
            "non-superuser role that owns nothing and has no BYPASSRLS to make the "
            "policies effective.",
            status["policy_count"], status["role"],
        )
    elif status["enforced"]:
        logger.info(
            "row-level security active: %d policies enforced for role %r",
            status["policy_count"], status["role"],
        )
    return status
