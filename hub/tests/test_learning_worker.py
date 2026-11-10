"""The durable worker survives failures, retries recording and remains observable."""

import asyncio
import contextlib
import json
from dataclasses import replace

import pytest
from starlette.applications import Starlette

from hub import learning, observability, server

pytestmark = pytest.mark.asyncio


async def test_evaluation_and_failure_recording_outage_recovers_without_restart(monkeypatch):
    stop = asyncio.Event()
    events = []
    claim = ("org", "experiment", 1, "lease")

    async def claim_work(_factory):
        events.append("claim")
        return claim

    async def complete(_factory, _claim):
        events.append("evaluate")
        if events.count("evaluate") == 1:
            raise RuntimeError("evaluation unavailable")
        stop.set()
        return True

    class Session:
        async def execute(self, _statement):
            pass

    @contextlib.asynccontextmanager
    async def recording_session(_factory, *, org_id):
        assert org_id == claim[0]
        if "record-failed" not in events:
            events.append("record-failed")
            raise OSError("database unavailable")
        events.append("record-ok")
        yield Session()

    monkeypatch.setattr(learning, "claim_work", claim_work)
    monkeypatch.setattr(learning, "complete_work", complete)
    monkeypatch.setattr(learning, "session_scope", recording_session)
    await asyncio.wait_for(learning.run_worker(None, stop_event=stop, interval_seconds=0.001), 2)
    assert events == ["claim", "evaluate", "record-failed", "record-ok", "claim", "evaluate"]


@pytest.mark.parametrize("raises", [False, True])
async def test_supervisor_restarts_an_unexpected_exit_and_preserves_stop(monkeypatch, raises):
    stop = asyncio.Event()
    health = {}
    calls = []

    async def worker(_factory, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            if raises:
                raise OSError("unexpected worker exit")
            return
        assert kwargs["health"] is health
        stop.set()

    monkeypatch.setattr(learning, "run_worker", worker)
    await asyncio.wait_for(learning.supervise_worker(None, stop_event=stop, interval_seconds=0.001, health=health), 2)
    assert len(calls) == 2 and health["restarts"] == 1 and not health["running"]


async def test_worker_cancellation_propagates_without_restart(monkeypatch):
    started = asyncio.Event()
    health = {}

    async def claim(_factory):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(learning, "claim_work", claim)
    task = asyncio.create_task(learning.supervise_worker(None, stop_event=asyncio.Event(), health=health))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert health["restarts"] == 0 and not health["running"] and not health["healthy"]


async def test_learning_worker_health_controls_readiness_and_metrics():
    state = {"running": True, "healthy": False, "failures": 2, "restarts": 1}

    class Session:
        async def execute(self, _sql):
            pass

    @contextlib.asynccontextmanager
    async def factory():
        yield Session()

    app = Starlette()
    observability.add_health_routes(app, factory, worker_health=lambda: state)
    routes = {route.path: route.endpoint for route in app.routes}
    failed = await routes["/readyz"](None)
    assert failed.status_code == 503 and json.loads(failed.body)["learning_worker"] == "unavailable"
    metrics = (await routes["/metrics"](None)).body.decode()
    assert "unimem_learning_worker_up 0" in metrics
    assert "unimem_learning_worker_restarts_total 1" in metrics
    state["healthy"] = True
    assert (await routes["/readyz"](None)).status_code == 200
    assert "unimem_learning_worker_up 1" in (await routes["/metrics"](None)).body.decode()


async def test_server_lifespan_wires_supervision_and_readiness(session_factory, config, monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()
    observed = []

    async def supervisor(_factory, *, stop_event, health):
        observed.append(health)
        health.update(running=True, healthy=True)
        started.set()
        try:
            await stop_event.wait()
        finally:
            health.update(running=False, healthy=False)
            stopped.set()

    monkeypatch.setattr(learning, "supervise_worker", supervisor)
    config = replace(config, allow_rls_bypass=True, alert_scheduler_enabled=False, webhook_scheduler_enabled=False)
    app = server.build_app(config, session_factory)
    ready = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/readyz")
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), 2)
        assert app.state.learning_worker_health is observed[0]
        assert (await ready(None)).status_code == 200
        observed[0]["healthy"] = False
        assert (await ready(None)).status_code == 503
    assert stopped.is_set() and not observed[0]["running"]
