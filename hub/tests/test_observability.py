from __future__ import annotations

import json
import logging

import pytest
from sqlalchemy import text

from hub import observability


class TestMetricsCardinalityIsBounded:
    def test_an_unknown_path_collapses_to_one_bucket(self):
        metrics = observability.Metrics()
        for i in range(50):
            metrics.observe_request("GET", f"/aaaa{i}", 404, 1.0)
        rendered = metrics.render()
        assert 'path="other"' in rendered
        assert "aaaa" not in rendered

    def test_an_unknown_method_collapses_to_one_bucket(self):
        metrics = observability.Metrics()
        for i in range(50):
            metrics.observe_request(f"FOOBAR{i}", "/mcp", 404, 1.0)
        rendered = metrics.render()
        assert 'method="OTHER"' in rendered
        assert "FOOBAR" not in rendered

    def test_the_internal_dict_stays_bounded_regardless_of_attacker_input(self):
        metrics = observability.Metrics()
        for i in range(500):
            metrics.observe_request(f"VERB{i}", f"/path{i}", 404, 1.0)
        assert len(metrics._requests) == 1

    def test_a_known_method_and_path_are_labelled_precisely(self):
        metrics = observability.Metrics()
        metrics.observe_request("POST", "/mcp", 200, 5.0)
        rendered = metrics.render()
        assert 'method="POST",path="/mcp",status="200"' in rendered


class TestDurationHistogram:
    def test_bucket_counts_are_cumulative(self):
        metrics = observability.Metrics()
        metrics.observe_request("GET", "/mcp", 200, 5.0)
        rendered = metrics.render()
        assert 'duration_ms_bucket{path="/mcp",le="1"} 0' in rendered
        assert 'duration_ms_bucket{path="/mcp",le="2"} 0' in rendered
        assert 'duration_ms_bucket{path="/mcp",le="5"} 1' in rendered
        assert 'duration_ms_bucket{path="/mcp",le="10"} 1' in rendered
        assert 'duration_ms_bucket{path="/mcp",le="+Inf"} 1' in rendered

    def test_sum_and_count_match_the_raw_observations(self):
        metrics = observability.Metrics()
        metrics.observe_request("GET", "/mcp", 200, 3.0)
        metrics.observe_request("GET", "/mcp", 200, 7.0)
        metrics.observe_request("GET", "/mcp", 200, 40.0)
        rendered = metrics.render()
        assert 'duration_ms_sum{path="/mcp"} 50.00' in rendered
        assert 'duration_ms_count{path="/mcp"} 3' in rendered
        assert 'duration_ms_bucket{path="/mcp",le="10"} 2' in rendered
        assert 'duration_ms_bucket{path="/mcp",le="+Inf"} 3' in rendered

    def test_an_observation_past_the_largest_finite_bucket_only_counts_in_inf(self):
        metrics = observability.Metrics()
        metrics.observe_request("GET", "/mcp", 200, 60_000.0)
        rendered = metrics.render()
        assert f'duration_ms_bucket{{path="/mcp",le="{observability.Metrics.BUCKETS_MS[-1]:g}"}} 0' in rendered
        assert 'duration_ms_bucket{path="/mcp",le="+Inf"} 1' in rendered

    def test_paths_have_independent_histograms(self):
        metrics = observability.Metrics()
        metrics.observe_request("GET", "/mcp", 200, 5.0)
        metrics.observe_request("GET", "/healthz", 200, 5000.0)
        rendered = metrics.render()
        assert 'duration_ms_count{path="/mcp"} 1' in rendered
        assert 'duration_ms_count{path="/healthz"} 1' in rendered
        assert 'duration_ms_bucket{path="/mcp",le="5"} 1' in rendered
        assert 'duration_ms_bucket{path="/healthz",le="5"} 0' in rendered


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
        observability.configure_logging("INFO")
        first = len(logging.getLogger().handlers)
        observability.configure_logging("INFO")
        assert len(logging.getLogger().handlers) == first == 1


@pytest.mark.asyncio
class TestHealthAndReadiness:
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
        def _broken():
            raise OSError("could not connect to server")

        response = await self._endpoint(_broken, "/readyz")(None)
        assert response.status_code == 503
        assert json.loads(response.body)["database"] == "unreachable"

    async def test_liveness_and_readiness_are_separate_routes(self, session_factory):
        from starlette.applications import Starlette

        app = Starlette()
        observability.add_health_routes(app, session_factory)
        paths = {getattr(r, "path", None) for r in app.routes}
        assert {"/healthz", "/readyz"} <= paths

    async def test_readyz_is_rate_limited_per_client(self, session_factory):
        from dataclasses import dataclass

        from starlette.applications import Starlette

        from hub.abuse import RateLimiter

        @dataclass
        class _FakeClient:
            host: str

        @dataclass
        class _FakeRequest:
            client: _FakeClient

        app = Starlette()
        observability.add_health_routes(
            app, session_factory, readyz_rate_limiter=RateLimiter(per_minute=60, burst=1)
        )
        readyz = next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/readyz")

        async with session_factory() as _warmup_session:
            await _warmup_session.execute(text("SELECT 1"))

        request = _FakeRequest(client=_FakeClient(host="1.2.3.4"))
        first = await readyz(request)
        second = await readyz(request)
        assert first.status_code == 200
        assert second.status_code == 429


@pytest.mark.asyncio
class TestSecurityResponseHeaders:
    async def test_every_response_carries_the_baseline_headers(self):
        import httpx
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def _ok(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/x", _ok)])
        app.add_middleware(observability.RequestContextMiddleware)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/x")

        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Referrer-Policy"] == "no-referrer"
        assert "max-age" in r.headers["Strict-Transport-Security"]


class TestTransportSafety:
    def test_refuses_non_loopback_host_by_default(self):
        from hub.config import HubConfig

        config = HubConfig(database_url="postgresql+asyncpg://x/y", host="0.0.0.0")
        with pytest.raises(RuntimeError):
            config.validate_transport_safety()

    def test_loopback_host_is_always_fine(self):
        from hub.config import HubConfig

        for host in ("127.0.0.1", "localhost", "::1"):
            HubConfig(database_url="postgresql+asyncpg://x/y", host=host).validate_transport_safety()

    def test_explicit_opt_in_allows_non_loopback_host(self):
        from hub.config import HubConfig

        config = HubConfig(
            database_url="postgresql+asyncpg://x/y", host="0.0.0.0", allow_insecure_http=True,
        )
        config.validate_transport_safety()


class TestMetricsToken:
    @staticmethod
    async def _get(token: str, headers: dict | None = None):
        import httpx
        from starlette.applications import Starlette

        def _no_db():
            raise AssertionError("/metrics must not touch the database")

        app = Starlette()
        observability.add_health_routes(app, _no_db, metrics_token=token)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await c.get("/metrics", headers=headers or {})

    @pytest.mark.asyncio
    async def test_unset_it_stays_open(self):
        assert (await self._get("")).status_code == 200

    @pytest.mark.asyncio
    async def test_set_it_requires_the_bearer_token(self):
        assert (await self._get("scrape-secret")).status_code == 401
        wrong = await self._get("scrape-secret", {"Authorization": "Bearer nope"})
        assert wrong.status_code == 401 and "Bearer" in wrong.headers["www-authenticate"]
        assert (await self._get("scrape-secret", {"Authorization": "Basic scrape-secret"})).status_code == 401
        right = await self._get("scrape-secret", {"Authorization": "Bearer scrape-secret"})
        assert right.status_code == 200 and right.headers["content-type"].startswith("text/plain")

    def test_it_is_read_from_the_environment(self, monkeypatch):
        from hub.config import HubConfig

        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv("HUB_METRICS_TOKEN", "from-env")
        assert HubConfig.from_env().metrics_token == "from-env"


class TestNoSecretsInRequestLogs:
    @pytest.mark.asyncio
    async def test_a_share_token_in_the_path_is_redacted(self, caplog):
        import httpx
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def _ok(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/app/proof/shared/{token}", _ok), Route("/app/memory", _ok)])
        app.add_middleware(observability.RequestContextMiddleware)
        caplog.set_level(logging.INFO, logger="commontrace.hub.request")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            await c.get("/app/proof/shared/SECRET-TOKEN-abc")
            await c.get("/app/memory?q=customer+search+text")
        hub_records = [r for r in caplog.records if r.name == "commontrace.hub.request"]
        assert [r.http_path for r in hub_records] == ["/app/proof/shared/[redacted]", "/app/memory"]
        logged = " ".join(str(vars(r)) for r in hub_records)
        assert "SECRET-TOKEN" not in logged and "customer" not in logged

    def test_uvicorn_writes_no_access_log_of_its_own(self, monkeypatch):
        import uvicorn

        from hub import main
        from hub.config import HubConfig

        seen = {}
        config = HubConfig(database_url="postgresql+asyncpg://x/y")
        monkeypatch.setattr(main, "build_server_app", lambda: (config, object()))
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
        main.main()
        assert seen["access_log"] is False
