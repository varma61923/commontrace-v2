"""Startup, probes and shutdown must stay safe during dependency failures."""

import asyncio
import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.requests import Request

from hub import main, observability, server
from hub.abuse import RateLimiter
from hub.config import HubConfig


@pytest.mark.parametrize("suite", ["tests/test_ui_browser.py", "hub/tests/test_console_accessibility.py"])
@pytest.mark.parametrize("required", [False, True])
def test_missing_playwright_fails_when_browser_verification_is_required(suite, required):
    root = Path(__file__).resolve().parents[2]
    program = """
import importlib.abc
import runpy
import sys

class MissingPlaywright(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'playwright' or fullname.startswith('playwright.'):
            raise ModuleNotFoundError('Playwright deliberately unavailable', name=fullname)

sys.meta_path.insert(0, MissingPlaywright())
runpy.run_path(sys.argv[1])
"""
    env = dict(os.environ)
    env.pop("UNIMEM_REQUIRE_BROWSER", None)
    if required:
        env["UNIMEM_REQUIRE_BROWSER"] = "1"
    result = subprocess.run([sys.executable, "-c", program, suite], cwd=root, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert ("ModuleNotFoundError:" if required else "Skipped:") in result.stderr
    assert ("Skipped:" in result.stderr) is not required


def endpoints(factory, **kwargs):
    app = Starlette()
    observability.add_health_routes(app, factory, **kwargs)
    return {route.path: route.endpoint for route in app.routes}


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["limiter", "connect", "query"])
async def test_readiness_times_out_dependency_work_and_releases_sessions(blocked):
    closed = []

    class Session:
        async def execute(self, _sql):
            if blocked == "query":
                await asyncio.sleep(3600)

    @contextlib.asynccontextmanager
    async def factory():
        try:
            if blocked == "connect":
                await asyncio.sleep(3600)
            yield Session()
        finally:
            closed.append(True)

    class Limiter:
        async def check(self, _key):
            if blocked == "limiter":
                await asyncio.sleep(3600)
            return True, 0

    ready = endpoints(factory, readyz_rate_limiter=Limiter(), readiness_timeout_seconds=0.01)["/readyz"]
    response = await asyncio.wait_for(ready(None), timeout=1)
    assert response.status_code == 503
    assert json.loads(response.body)["database"] == "unreachable"
    assert closed == ([] if blocked == "limiter" else [True])


@pytest.mark.asyncio
async def test_metrics_cannot_consume_the_readiness_budget_or_use_its_database_limiter():
    class Session:
        async def execute(self, _sql):
            return None

    @contextlib.asynccontextmanager
    async def factory():
        yield Session()

    routes = endpoints(factory, readyz_rate_limiter=RateLimiter(per_minute=60, burst=1), metrics_token="scrape")
    for _ in range(35):
        assert (await routes["/metrics"](None)).status_code == 401
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer scrape")]})
    assert (await routes["/metrics"](request)).status_code == 200
    assert (await routes["/readyz"](None)).status_code == 200
    assert (await routes["/readyz"](None)).status_code == 429


@pytest.mark.asyncio
async def test_readiness_limiter_failure_is_a_controlled_503_and_does_not_log_credentials(caplog):
    class Limiter:
        async def check(self, _key):
            raise OSError("postgresql://operator:private-password@db")

    ready = endpoints(lambda: None, readyz_rate_limiter=Limiter())["/readyz"]
    assert (await ready(None)).status_code == 503
    assert "private-password" not in caplog.text


@pytest.mark.asyncio
async def test_zero_shutdown_grace_cancels_instead_of_waiting_forever():
    task = asyncio.create_task(asyncio.sleep(3600))
    try:
        await asyncio.wait_for(server._join_background_tasks([task], 0), timeout=1)
        assert task.cancelled()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["startup", "shutdown", "none"])
async def test_engine_ownership_covers_the_entire_application_lifespan(monkeypatch, failure):
    disposed = []

    async def dispose():
        disposed.append(True)

    @contextlib.asynccontextmanager
    async def previous(_app):
        if failure == "startup":
            raise RuntimeError("startup refused")
        yield
        if failure == "shutdown":
            raise RuntimeError("shutdown failed")

    app = Starlette(lifespan=previous)
    monkeypatch.setattr(main.HubConfig, "from_env", lambda: HubConfig(database_url="postgresql+asyncpg://x/y"))
    monkeypatch.setattr(main, "configure_logging", lambda *_: None)
    monkeypatch.setattr(main, "make_engine", lambda _: SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(main, "make_session_factory", lambda _: None)
    monkeypatch.setattr(main, "build_app", lambda *_: app)
    _, app = main.build_server_app()
    if failure == "none":
        async with app.router.lifespan_context(app):
            pass
    else:
        with pytest.raises(RuntimeError, match=failure):
            async with app.router.lifespan_context(app):
                pass
    assert disposed == [True]


@pytest.mark.asyncio
async def test_projection_factory_closes_when_startup_rls_validation_fails(monkeypatch):
    closed = []

    async def close():
        closed.append(True)

    async def fail(*_args, **_kwargs):
        raise RuntimeError("RLS validation failed")

    app = Starlette()
    app.state.memory_projection_factory = SimpleNamespace(close=close)
    mcp = SimpleNamespace(streamable_http_app=lambda **_: app)
    monkeypatch.setattr(server, "build_mcp_server", lambda *_: mcp)
    monkeypatch.setattr(server, "check_row_level_security", fail)
    app = server.build_app(HubConfig(database_url="postgresql+asyncpg://x/y"), lambda: None)
    with pytest.raises(RuntimeError, match="RLS validation"):
        async with app.router.lifespan_context(app):
            pass
    assert closed == [True]


def test_request_ids_reject_terminal_newlines():
    assert observability._resolve_request_id("caller-id\n") != "caller-id\n"


@pytest.mark.parametrize("raw", ["0", "61", "nan", "true"])
def test_invalid_readiness_deadlines_are_rejected_at_configuration_load(monkeypatch, raw):
    monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://x/y")
    monkeypatch.setenv("HUB_READYZ_TIMEOUT_SECONDS", raw)
    with pytest.raises(ValueError, match="HUB_READYZ_TIMEOUT_SECONDS"):
        HubConfig.from_env()
