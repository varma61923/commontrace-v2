"""`build_app` is the production entrypoint, and nothing was testing it.

This file exists because of a specific escape. A startup hook was added to
`build_app` using `Starlette.add_event_handler`, an API Starlette has
since removed. The full hub suite -- 1127 tests, on the same Starlette
version that lacks the attribute -- passed, and the container crash-looped
on boot:

    AttributeError: 'Starlette' object has no attribute 'add_event_handler'

The suite could not have caught it. Every test that needs an app builds a
small `Starlette(routes=[...])` of its own and wraps the specific
middleware under test; not one called `build_app`, so its wiring was
exercised for the first time by `docker image builds and starts`. That
check did its job, but "the container boots" is a slow and coarse way to
learn that an app factory raises.

So these tests do the cheap version of what the docker job does: build the
real app from a real config and drive its lifespan, which is where startup
wiring actually runs. A lifespan is not optional decoration -- an ASGI
server sends `lifespan.startup` before it accepts a request, so anything
raising there means the process never serves.
"""
from __future__ import annotations

import dataclasses

import pytest

from hub.server import build_app

pytestmark = pytest.mark.asyncio


async def _run_lifespan(app) -> list[dict]:
    """Drive the app's lifespan protocol directly, collecting what it sends.

    Raw ASGI rather than a test client: a client that swallows a failed
    startup into a 500 would hide exactly the failure this file is about.
    """
    received: list[dict] = []
    to_send = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def receive():
        return to_send.pop(0) if to_send else {"type": "lifespan.shutdown"}

    async def send(message):
        received.append(message)

    await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    return received


class TestTheProductionAppBoots:
    async def test_build_app_returns_an_app_at_all(self, config, session_factory):
        """The plainest regression for the crash: `build_app` raised before
        it ever returned, so the process died at import-and-build time,
        before any request or even any lifespan event."""
        app = build_app(config, session_factory)
        assert app is not None

    async def test_the_lifespan_starts_and_shuts_down_cleanly(self, config, session_factory):
        """Where startup wiring actually runs. `lifespan.startup.failed`
        rather than `.complete` is what an ASGI server sees when a startup
        hook raises -- and it refuses to serve rather than starting
        degraded, which is why this is a boot failure and not a warning."""
        app = build_app(config, session_factory)
        messages = await _run_lifespan(app)
        types = [m["type"] for m in messages]
        assert "lifespan.startup.complete" in types, (
            f"startup did not complete: {messages}"
        )
        assert "lifespan.startup.failed" not in types

    async def test_the_mcp_session_manager_lifespan_is_not_replaced(
        self, config, session_factory
    ):
        """The composition this file's fix depends on. The MCP app installs
        its own lifespan for the session manager; anything chaining onto it
        must wrap that, not overwrite it. Overwriting is silent -- the app
        still boots, and the session manager is simply never started."""
        app = build_app(config, session_factory)
        assert app.router.lifespan_context is not None
        messages = await _run_lifespan(app)
        assert "lifespan.shutdown.complete" in [m["type"] for m in messages]

    async def test_it_boots_with_the_commons_surface_disabled(
        self, config, session_factory
    ):
        """The other build_app branch. A config flag that changes which
        routes and tools are wired is exactly the kind of thing that boots
        in one shape and raises in the other."""
        cfg = dataclasses.replace(config, commons_enabled=False)
        messages = await _run_lifespan(build_app(cfg, session_factory))
        assert "lifespan.startup.complete" in [m["type"] for m in messages]

    async def test_it_boots_with_the_postgres_rate_limiter_backend(
        self, config, session_factory
    ):
        """HUB_RATE_LIMIT_BACKEND=postgres is the documented multi-replica
        configuration (hub/DEPLOYMENT.md §6) and wires up different limiter
        objects, each opening a pool. Booting under the default only is how
        a backend-specific wiring error reaches a deployment."""
        cfg = dataclasses.replace(config, rate_limit_backend="postgres")
        messages = await _run_lifespan(build_app(cfg, session_factory))
        assert "lifespan.startup.complete" in [m["type"] for m in messages]

    async def test_it_boots_and_shuts_down_cleanly_with_the_alert_scheduler_enabled(
        self, config, session_factory
    ):
        """hub/scheduler.py is off by default; enabling it starts a
        background asyncio task from the lifespan and must join it again on
        shutdown. A short interval so a bug that left the loop sleeping
        through the stop event would show up as this test hanging, not
        merely as a slow one."""
        cfg = dataclasses.replace(
            config, alert_scheduler_enabled=True, alert_scheduler_interval_seconds=10,
        )
        messages = await _run_lifespan(build_app(cfg, session_factory))
        types = [m["type"] for m in messages]
        assert "lifespan.startup.complete" in types
        assert "lifespan.shutdown.complete" in types
        assert "lifespan.startup.failed" not in types

    async def test_it_boots_and_shuts_down_cleanly_with_the_webhook_scheduler_enabled(
        self, config, session_factory
    ):
        """The webhook-delivery counterpart to the alert scheduler test
        above: also off by default, also a background asyncio task the
        lifespan must join on shutdown."""
        cfg = dataclasses.replace(
            config, webhook_scheduler_enabled=True, webhook_scheduler_interval_seconds=5,
        )
        messages = await _run_lifespan(build_app(cfg, session_factory))
        types = [m["type"] for m in messages]
        assert "lifespan.startup.complete" in types
        assert "lifespan.shutdown.complete" in types
        assert "lifespan.startup.failed" not in types

    async def test_it_boots_with_both_schedulers_enabled(self, config, session_factory):
        """Both loops run from the same lifespan concurrently -- proves
        `asyncio.gather` on shutdown joins both tasks rather than only
        the first one wired in."""
        cfg = dataclasses.replace(
            config,
            alert_scheduler_enabled=True, alert_scheduler_interval_seconds=5,
            webhook_scheduler_enabled=True, webhook_scheduler_interval_seconds=5,
        )
        messages = await _run_lifespan(build_app(cfg, session_factory))
        types = [m["type"] for m in messages]
        assert "lifespan.startup.complete" in types
        assert "lifespan.shutdown.complete" in types
        assert "lifespan.startup.failed" not in types

    async def test_it_boots_with_an_ip_allowlist_configured(self, config, session_factory):
        """IpAllowlistMiddleware is only mounted when HUB_IP_ALLOWLIST is
        set -- proves the conditional wiring in build_app itself doesn't
        raise, same as the commons_enabled=False / postgres-backend cases
        above cover their own conditional branches."""
        cfg = dataclasses.replace(config, ip_allowlist=("10.0.0.0/8",))
        messages = await _run_lifespan(build_app(cfg, session_factory))
        assert "lifespan.startup.complete" in [m["type"] for m in messages]


class TestStartupToleratesADeadDatabase:
    async def test_the_app_still_boots_when_the_database_is_unreachable(
        self, config
    ):
        """Startup diagnostics must not become a boot dependency.

        The RLS check runs a query at startup. If an unreachable database
        turned that into a crash, a replica would refuse to start during
        exactly the incident where being up to serve health checks and
        cached work matters -- and it would look like an application bug
        rather than a database outage.
        """
        from hub.db import make_engine, make_session_factory

        dead = dataclasses.replace(
            config,
            database_url="postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nope",
            db_statement_timeout_ms=0,
        )
        engine = make_engine(dead)
        try:
            app = build_app(dead, make_session_factory(engine))
            messages = await _run_lifespan(app)
            assert "lifespan.startup.complete" in [m["type"] for m in messages]
        finally:
            await engine.dispose()
