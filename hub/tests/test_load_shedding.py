"""The Hub bounds what it accepts instead of queueing until it all fails.

hub/bench_concurrency.py measured the shape these bounds exist for: at 128
concurrent clients against an empty handler, p99 reached 1.3s and
throughput was flat from 8 clients up, so every additional client became
queue -- with nothing anywhere to stop that queue growing. Add a slow
database and requests pile onto a 10+5 connection pool with a 30s
pool_timeout until they fail together, which is the worst failure shape to
operate: the first symptom is total failure rather than degradation.

These tests pin the behaviour, not the numbers: that the cap refuses
rather than waits (a cap that queues is not a cap), that a refusal says
when to come back, that a hung request is cut off as a 504 rather than
hanging or 500ing, and that both bounds can be turned off for a
deployment that does this at its edge proxy instead.
"""
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
        """The property that makes this a cap and not a queue: with one
        permit and one request already in flight, the second comes back
        refused *while the first is still running*, not after it."""
        app = _app(max_concurrent=1, timeout_seconds=5, delay=0.25)
        async with _client(app) as c:
            first = asyncio.create_task(c.get("/x"))
            await asyncio.sleep(0.05)  # let the first take the only permit
            second = await c.get("/x")
            assert second.status_code == 503
            assert (await first).status_code == 200

    async def test_a_refusal_says_when_to_come_back(self):
        """Same reasoning as _rate_limited_response: a refused client with
        no Retry-After can only guess, and guessing short against an
        overloaded replica turns one burst into a sustained one."""
        app = _app(max_concurrent=1, timeout_seconds=5, delay=0.25)
        async with _client(app) as c:
            first = asyncio.create_task(c.get("/x"))
            await asyncio.sleep(0.05)
            second = await c.get("/x")
            assert second.headers["Retry-After"] == "1"
            assert second.json()["error"] == "overloaded"
            await first

    async def test_capacity_is_released_so_the_cap_is_not_a_one_shot_fuse(self):
        """A semaphore leaked on the served path would make the replica
        refuse everything forever after its first N requests -- a far worse
        failure than the queueing this replaces."""
        async with _client(_app(max_concurrent=1, timeout_seconds=5)) as c:
            for _ in range(5):
                assert (await c.get("/x")).status_code == 200

    async def test_zero_disables_the_cap_entirely(self):
        """For a deployment that sheds at its edge proxy and does not want
        two layers disagreeing about which one refused a request."""
        app = _app(max_concurrent=0, timeout_seconds=5, delay=0.1)
        async with _client(app) as c:
            results = await asyncio.gather(*(c.get("/x") for _ in range(8)))
        assert [r.status_code for r in results] == [200] * 8


class TestTheRequestTimeout:
    async def test_a_request_that_finishes_in_time_is_untouched(self):
        async with _client(_app(max_concurrent=0, timeout_seconds=5, delay=0.01)) as c:
            assert (await c.get("/x")).status_code == 200

    async def test_a_request_that_overruns_is_cut_off_as_a_gateway_timeout(self):
        """504 rather than 500: nothing is known to be broken, the request
        ran out of time -- a different thing to tell a client, and the only
        one of the two they can sensibly retry."""
        async with _client(_app(max_concurrent=0, timeout_seconds=1, delay=30)) as c:
            response = await asyncio.wait_for(c.get("/x"), timeout=10)
        assert response.status_code == 504
        assert response.json()["error"] == "timeout"

    async def test_the_request_is_actually_cut_off_not_just_relabelled(self):
        """The property the first implementation of this middleware did NOT
        have, which is why it is pinned by the clock rather than by the
        status code alone.

        Built on BaseHTTPMiddleware, wrapping `call_next` in wait_for
        cancelled the middleware's own plumbing but left the downstream
        handler running to completion: a 1s timeout over a 4s handler
        returned its 504 after 4.01s. The status code was right and the
        bound was worthless -- the replica still paid the full cost, so
        nothing was shed and no resource was freed any sooner.

        A status-only assertion passes in both worlds. Only the elapsed
        time tells them apart, so that is what this measures: the response
        must arrive on the timeout's schedule, not the handler's."""
        app = _app(max_concurrent=0, timeout_seconds=1, delay=8)
        async with _client(app) as c:
            start = time.perf_counter()
            response = await asyncio.wait_for(c.get("/x"), timeout=20)
            elapsed = time.perf_counter() - start
        assert response.status_code == 504
        # Generously bounded -- the point is 1s-not-8s, not sub-second
        # precision on a loaded CI machine.
        assert elapsed < 4.0, f"504 arrived after {elapsed:.2f}s; the handler was not cut off"

    async def test_the_handler_is_cancelled_not_merely_abandoned(self):
        """"Cut off" has to mean the work stops, not that the caller stops
        waiting for it. Those look identical from the client side -- both
        return a prompt 504 -- but only one frees the connection, the
        transaction and the CPU the request was holding. If the handler
        merely kept running unobserved, the replica would shed nothing
        under exactly the overload this exists for, and would additionally
        be doing work whose result nobody can receive.

        Distinguished by whether the handler's own cancellation path runs
        and whether it ever reaches the far side of its wait."""
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

        # Give an abandoned-but-live handler every chance to finish and
        # set the flag, so this fails loudly rather than racing.
        await asyncio.sleep(0.2)
        assert was_cancelled, "the handler was abandoned, not cancelled"
        assert not reached_end, "the handler ran to completion after the client got its 504"

    async def test_zero_disables_the_timeout(self):
        async with _client(_app(max_concurrent=0, timeout_seconds=0, delay=0.05)) as c:
            assert (await c.get("/x")).status_code == 200

    async def test_a_timed_out_request_does_not_leak_its_capacity(self):
        """The two bounds interacting: if the timeout path skipped
        releasing the semaphore, one slow request would permanently shrink
        the replica's capacity."""
        app = _app(max_concurrent=1, timeout_seconds=1, delay=30)
        async with _client(app) as c:
            assert (await asyncio.wait_for(c.get("/x"), timeout=10)).status_code == 504

        fast = _app(max_concurrent=1, timeout_seconds=5)
        async with _client(fast) as c:
            assert (await c.get("/x")).status_code == 200


class TestStatementTimeoutIsConfigured:
    """Asserted against a real connection rather than SQLAlchemy's
    internals: what matters is the value Postgres ends up enforcing, and
    poking at `create_connect_args` proved to assert the wrong thing --
    it reports the URL-derived arguments and never sees `connect_args` at
    all, so it passed or failed for reasons unrelated to the setting."""

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
        # Postgres normalises the raw milliseconds into its own units.
        assert value in ("1234ms", "1234")

    async def test_zero_leaves_the_servers_own_default_alone(self, config):
        """For a deployment that sets statement_timeout on the database
        role instead; the Hub must not silently override that."""
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
