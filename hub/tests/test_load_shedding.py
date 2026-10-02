from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from hub.server import LoadShedMiddleware

pytestmark = pytest.mark.asyncio


def _app(*, max_concurrent: int, timeout_seconds: int, delay: float = 0.0) -> Starlette:
    async def handler(request):  # noqa: ARG001
        if delay:
            await asyncio.sleep(delay)
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/x", handler)])
    app.add_middleware(
        LoadShedMiddleware, max_concurrent=max_concurrent, timeout_seconds=timeout_seconds
    )
    return app


def _client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestTheConcurrencyCap:
    async def test_a_request_under_the_cap_is_served_normally(self):
        async with _client(_app(max_concurrent=4, timeout_seconds=5)) as c:
            response = await c.get("/x")
        assert response.status_code == 200

    async def test_past_the_cap_a_request_is_refused_rather_than_queued(self):
        app = _app(max_concurrent=1, timeout_seconds=5, delay=0.25)
        async with _client(app) as c:
            first = asyncio.create_task(c.get("/x"))
            await asyncio.sleep(0.05)
            second = await c.get("/x")
            assert second.status_code == 503
            assert (await first).status_code == 200

    async def test_a_refusal_says_when_to_come_back(self):
        app = _app(max_concurrent=1, timeout_seconds=5, delay=0.25)
        async with _client(app) as c:
            first = asyncio.create_task(c.get("/x"))
            await asyncio.sleep(0.05)
            second = await c.get("/x")
            assert second.headers["Retry-After"] == "1"
            assert second.json()["error"] == "overloaded"
            await first

    async def test_capacity_is_released_so_the_cap_is_not_a_one_shot_fuse(self):
        async with _client(_app(max_concurrent=1, timeout_seconds=5)) as c:
            for _ in range(5):
                assert (await c.get("/x")).status_code == 200

    async def test_zero_disables_the_cap_entirely(self):
        app = _app(max_concurrent=0, timeout_seconds=5, delay=0.1)
        async with _client(app) as c:
            results = await asyncio.gather(*(c.get("/x") for _ in range(8)))
        assert [r.status_code for r in results] == [200] * 8


class TestTheRequestTimeout:
    async def test_a_request_that_finishes_in_time_is_untouched(self):
        async with _client(_app(max_concurrent=0, timeout_seconds=5, delay=0.01)) as c:
            assert (await c.get("/x")).status_code == 200

    async def test_a_request_that_overruns_is_cut_off_as_a_gateway_timeout(self):
        async with _client(_app(max_concurrent=0, timeout_seconds=1, delay=30)) as c:
            response = await asyncio.wait_for(c.get("/x"), timeout=10)
        assert response.status_code == 504
        assert response.json()["error"] == "timeout"

    async def test_the_request_is_actually_cut_off_not_just_relabelled(self):
        app = _app(max_concurrent=0, timeout_seconds=1, delay=8)
        async with _client(app) as c:
            start = time.perf_counter()
            response = await asyncio.wait_for(c.get("/x"), timeout=20)
            elapsed = time.perf_counter() - start
        assert response.status_code == 504
        assert elapsed < 4.0, f"504 arrived after {elapsed:.2f}s; the handler was not cut off"

    async def test_the_handler_is_cancelled_not_merely_abandoned(self):
        reached_end = False
        was_cancelled = False

        async def handler(request):  # noqa: ARG001
            nonlocal reached_end, was_cancelled
            try:
                await asyncio.sleep(8)
            except asyncio.CancelledError:
                was_cancelled = True
                raise
            reached_end = True
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/x", handler)])
        app.add_middleware(LoadShedMiddleware, max_concurrent=0, timeout_seconds=1)
        async with _client(app) as c:
            assert (await asyncio.wait_for(c.get("/x"), timeout=20)).status_code == 504

        await asyncio.sleep(0.2)
        assert was_cancelled, "the handler was abandoned, not cancelled"
        assert not reached_end, "the handler ran to completion after the client got its 504"

    async def test_zero_disables_the_timeout(self):
        async with _client(_app(max_concurrent=0, timeout_seconds=0, delay=0.05)) as c:
            assert (await c.get("/x")).status_code == 200

    async def test_a_timed_out_request_does_not_leak_its_capacity(self):
        app = _app(max_concurrent=1, timeout_seconds=1, delay=30)
        async with _client(app) as c:
            assert (await asyncio.wait_for(c.get("/x"), timeout=10)).status_code == 504

        fast = _app(max_concurrent=1, timeout_seconds=5)
        async with _client(fast) as c:
            assert (await c.get("/x")).status_code == 200


class TestStatementTimeoutIsConfigured:
    async def test_postgres_enforces_the_configured_statement_timeout(self, config):
        import dataclasses

        from sqlalchemy import text

        from hub.db import make_engine

        cfg = dataclasses.replace(config, db_statement_timeout_ms=1234)
        engine = make_engine(cfg)
        try:
            async with engine.connect() as conn:
                value = (await conn.execute(text("SHOW statement_timeout"))).scalar_one()
        finally:
            await engine.dispose()
        assert value in ("1234ms", "1234")

    async def test_zero_leaves_the_servers_own_default_alone(self, config):
        import dataclasses

        from sqlalchemy import text

        from hub.db import make_engine

        cfg = dataclasses.replace(config, db_statement_timeout_ms=0)
        engine = make_engine(cfg)
        try:
            async with engine.connect() as conn:
                value = (await conn.execute(text("SHOW statement_timeout"))).scalar_one()
        finally:
            await engine.dispose()
        assert value != "1234ms"
