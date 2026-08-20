"""Async SQLAlchemy engine/session plumbing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from hub.config import HubConfig


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
    return create_async_engine(
        config.database_url,
        pool_pre_ping=True,
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
    raises mid-way through an abuse-control check)."""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
