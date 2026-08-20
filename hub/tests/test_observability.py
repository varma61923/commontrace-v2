"""Tests for hub/observability.py: JSON log formatting, request-id
propagation, and the liveness/readiness split."""
from __future__ import annotations

import json
import logging

import pytest

from hub import observability


class TestJsonLogFormatter:
    def _record(self, **kwargs):
        record = logging.LogRecord(
            name="test.logger", level=logging.INFO, pathname="p", lineno=1,
            msg=kwargs.pop("msg", "hello"), args=(), exc_info=None,
        )
        for k, v in kwargs.items():
            setattr(record, k, v)
        return record

    def test_emits_valid_json_with_core_fields(self):
        out = json.loads(observability.JsonLogFormatter().format(self._record()))
        assert out["message"] == "hello"
        assert out["level"] == "INFO"
        assert out["logger"] == "test.logger"
        assert "ts" in out

    def test_extra_fields_are_merged(self):
        out = json.loads(
            observability.JsonLogFormatter().format(self._record(http_status=503, duration_ms=12.5))
        )
        assert out["http_status"] == 503
        assert out["duration_ms"] == 12.5

    def test_unserializable_extra_does_not_break_the_line(self):
        """A log line must never raise: an un-JSON-able value is repr'd, not
        allowed to take down the request that logged it."""
        out = json.loads(observability.JsonLogFormatter().format(self._record(weird=object())))
        assert "weird" in out and isinstance(out["weird"], str)

    def test_request_id_included_when_set(self):
        token = observability.current_request_id.set("req-abc")
        try:
            out = json.loads(observability.JsonLogFormatter().format(self._record()))
        finally:
            observability.current_request_id.reset(token)
        assert out["request_id"] == "req-abc"

    def test_request_id_absent_when_unset(self):
        assert "request_id" not in json.loads(
            observability.JsonLogFormatter().format(self._record())
        )


class TestResolveRequestId:
    """A client-supplied X-Request-ID is echoed verbatim into every log
    line and the response header -- it must be bounded and validated
    rather than accepted unconditionally."""

    def test_accepts_a_reasonable_client_supplied_id(self):
        assert observability._resolve_request_id("req-abc-123") == "req-abc-123"

    def test_accepts_a_uuid(self):
        rid = "550e8400-e29b-41d4-a716-446655440000"
        assert observability._resolve_request_id(rid) == rid

    def test_generates_one_when_absent(self):
        rid = observability._resolve_request_id(None)
        assert rid and rid != ""

    def test_rejects_an_oversized_id(self):
        oversized = "a" * 500
        rid = observability._resolve_request_id(oversized)
        assert rid != oversized
        assert len(rid) < len(oversized)

    def test_rejects_embedded_newline(self):
        hostile = "legit-id\nfake_log_line=injected"
        rid = observability._resolve_request_id(hostile)
        assert rid != hostile
        assert "\n" not in rid

    def test_rejects_embedded_control_characters(self):
        hostile = "id\x00\x1b[31mred"
        rid = observability._resolve_request_id(hostile)
        assert rid != hostile

    def test_rejects_empty_string(self):
        rid = observability._resolve_request_id("")
        assert rid != ""


class TestConfigureLogging:
    def test_is_idempotent(self):
        """Called twice (e.g. app reload) must not stack handlers and emit
        every line twice."""
        observability.configure_logging("INFO")
        first = len(logging.getLogger().handlers)
        observability.configure_logging("INFO")
        assert len(logging.getLogger().handlers) == first == 1


@pytest.mark.asyncio
class TestHealthAndReadiness:
    """Drives the real route handlers directly.

    Not via Starlette's TestClient: that spins up its own event loop, while
    the asyncpg pool from the `session_factory` fixture is bound to the
    loop the test is already running in. Calling the registered endpoint
    coroutines keeps everything on one loop, and since add_health_routes()
    *is* the unit under test, resolving the handler off the app's route
    table still exercises the wiring rather than bypassing it.
    """

    def _endpoint(self, session_factory, path: str):
        from starlette.applications import Starlette

        app = Starlette()
        observability.add_health_routes(app, session_factory)
        for route in app.routes:
            if getattr(route, "path", None) == path:
                return route.endpoint
        raise AssertionError(f"{path} was never registered by add_health_routes()")

    async def test_healthz_is_ok_and_does_not_touch_the_database(self):
        def _explode():
            raise AssertionError("liveness must not touch the database")

        response = await self._endpoint(_explode, "/healthz")(None)
        assert response.status_code == 200
        assert json.loads(response.body)["status"] == "ok"

    async def test_readyz_reports_ready_when_database_is_reachable(self, session_factory):
        response = await self._endpoint(session_factory, "/readyz")(None)
        assert response.status_code == 200
        assert json.loads(response.body)["database"] == "ok"

    async def test_readyz_returns_503_when_database_is_unreachable(self):
        """The actual bug this fixes: the old single /healthz returned 200
        even with Postgres down, so a load balancer kept sending traffic to
        an instance that could not serve one request."""

        def _broken():
            raise OSError("could not connect to server")

        response = await self._endpoint(_broken, "/readyz")(None)
        assert response.status_code == 503
        assert json.loads(response.body)["database"] == "unreachable"

    async def test_liveness_and_readiness_are_separate_routes(self, session_factory):
        """They must answer different questions -- wiring both probes to the
        same endpoint is the misconfiguration this split exists to prevent."""
        from starlette.applications import Starlette

        app = Starlette()
        observability.add_health_routes(app, session_factory)
        paths = {getattr(r, "path", None) for r in app.routes}
        assert {"/healthz", "/readyz"} <= paths
