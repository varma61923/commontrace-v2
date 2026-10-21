from __future__ import annotations

import dataclasses

import pytest

from hub.server import build_app

pytestmark = pytest.mark.asyncio


async def _run_lifespan(app) -> list[dict]:
    received: list[dict] = []
    to_send = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def receive():
        return to_send.pop(0) if to_send else {"type": "lifespan.shutdown"}

    async def send(message):
        received.append(message)

    await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    return received


class TestTheProductionAppBoots:
    async def test_a_missing_api_key_pepper_is_warned_about(self, config, session_factory, monkeypatch, caplog):
        from hub import auth

        monkeypatch.setattr(auth, "_pepper_env", "")
        with caplog.at_level("WARNING", logger="unimem.hub"):
            build_app(config, session_factory)
        assert "HUB_API_KEY_PEPPER is not set" in caplog.text
        caplog.clear()
        monkeypatch.setattr(auth, "_pepper_env", "a-shared-pepper")
        with caplog.at_level("WARNING", logger="unimem.hub"):
            build_app(config, session_factory)
        assert "HUB_API_KEY_PEPPER" not in caplog.text

    async def test_a_required_pepper_that_is_missing_refuses_to_start(self, config, session_factory, monkeypatch):
        from hub import auth

        strict = dataclasses.replace(config, require_api_key_pepper=True)
        monkeypatch.setattr(auth, "_pepper_env", "")
        with pytest.raises(RuntimeError, match="HUB_REQUIRE_API_KEY_PEPPER"):
            build_app(strict, session_factory)
        monkeypatch.setattr(auth, "_pepper_env", "a-shared-pepper")
        assert build_app(strict, session_factory) is not None

    async def test_build_app_returns_an_app_at_all(self, config, session_factory):
        app = build_app(config, session_factory)
        assert app is not None

    async def test_the_lifespan_starts_and_shuts_down_cleanly(self, config, session_factory):
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
        app = build_app(config, session_factory)
        assert app.router.lifespan_context is not None
        messages = await _run_lifespan(app)
        assert "lifespan.shutdown.complete" in [m["type"] for m in messages]

    async def test_it_boots_with_the_commons_surface_disabled(
        self, config, session_factory
    ):
        cfg = dataclasses.replace(config, commons_enabled=False)
        messages = await _run_lifespan(build_app(cfg, session_factory))
        assert "lifespan.startup.complete" in [m["type"] for m in messages]

    async def test_it_boots_and_shuts_down_with_the_verified_key_cache_on(
        self, config, session_factory
    ):
        from hub import auth

        cfg = dataclasses.replace(config, auth_cache_seconds=10)
        try:
            messages = await _run_lifespan(build_app(cfg, session_factory))
            types = [m["type"] for m in messages]
            assert "lifespan.startup.complete" in types
            assert "lifespan.shutdown.complete" in types
            assert auth._AUTH_CACHE_LISTENER_LIVE is False
        finally:
            auth.configure_auth_cache(0)

    async def test_it_boots_with_the_postgres_rate_limiter_backend(
        self, config, session_factory
    ):
        cfg = dataclasses.replace(config, rate_limit_backend="postgres")
        messages = await _run_lifespan(build_app(cfg, session_factory))
        assert "lifespan.startup.complete" in [m["type"] for m in messages]

    async def test_it_boots_and_shuts_down_cleanly_with_the_alert_scheduler_enabled(
        self, config, session_factory
    ):
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
        cfg = dataclasses.replace(
            config, webhook_scheduler_enabled=True, webhook_scheduler_interval_seconds=5,
        )
        messages = await _run_lifespan(build_app(cfg, session_factory))
        types = [m["type"] for m in messages]
        assert "lifespan.startup.complete" in types
        assert "lifespan.shutdown.complete" in types
        assert "lifespan.startup.failed" not in types

    async def test_it_boots_with_both_schedulers_enabled(self, config, session_factory):
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

    async def test_it_boots_with_the_rest_api_enabled(self, config, session_factory):
        cfg = dataclasses.replace(config, rest_api_enabled=True, signup_enabled=True)
        messages = await _run_lifespan(build_app(cfg, session_factory))
        assert "lifespan.startup.complete" in [m["type"] for m in messages]

    async def test_it_boots_with_the_rest_api_enabled_but_signup_off(
        self, config, session_factory
    ):
        cfg = dataclasses.replace(config, rest_api_enabled=True, signup_enabled=False)
        messages = await _run_lifespan(build_app(cfg, session_factory))
        assert "lifespan.startup.complete" in [m["type"] for m in messages]

    async def test_hosted_projection_resources_close_with_the_server(self, config, session_factory):
        cfg = dataclasses.replace(config, rest_api_enabled=True, signup_enabled=False)
        app = build_app(cfg, session_factory)

        class ProjectionResources:
            close_calls = 0

            async def close(self):
                self.close_calls += 1

        resources = ProjectionResources()
        app.state.memory_projection_factory = resources
        messages = await _run_lifespan(app)
        assert "lifespan.shutdown.complete" in [message["type"] for message in messages]
        assert resources.close_calls == 1

    async def test_it_boots_with_an_ip_allowlist_configured(self, config, session_factory):
        cfg = dataclasses.replace(config, ip_allowlist=("10.0.0.0/8",))
        messages = await _run_lifespan(build_app(cfg, session_factory))
        assert "lifespan.startup.complete" in [m["type"] for m in messages]


class TestStartupToleratesADeadDatabase:
    async def test_the_app_still_boots_when_the_database_is_unreachable(
        self, config
    ):
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


class TestEveryRouteBoundsItsRequestBody:
    async def test_an_oversized_body_is_refused_off_the_mcp_path_too(self, config, session_factory):
        import httpx

        app = build_app(config, session_factory)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/traces/search", content=b"x" * (config.max_request_body_bytes + 1),
                headers={"content-type": "application/json"},
            )
        assert response.status_code == 413
        assert response.json()["error"] == "payload_too_large"
        assert response.headers.get("x-request-id")
