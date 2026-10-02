from __future__ import annotations

import contextlib

import uvicorn

from hub.config import HubConfig
from hub.db import make_engine, make_session_factory
from hub.observability import configure_logging
from hub.server import build_app


def build_server_app():
    """Build the ASGI app + a lifespan that disposes the engine on shutdown."""
    config = HubConfig.from_env()
    config.validate_transport_safety()
    configure_logging(config.log_level)

    engine = make_engine(config)
    session_factory = make_session_factory(engine)
    app = build_app(config, session_factory)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        await engine.dispose()

    previous_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def chained(_app):
        async with previous_lifespan(_app):
            async with lifespan(_app):
                yield

    app.router.lifespan_context = chained
    return config, app


def main() -> None:
    config, app = build_server_app()
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=config.graceful_shutdown_seconds,
    )


if __name__ == "__main__":
    main()
