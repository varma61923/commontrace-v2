"""Run the Hub server: `python -m hub.main` (or `commontrace-hub` if the
package is installed).

Reads all configuration from the environment (see hub/.env.example) via
hub/config.py. Does not run migrations itself -- run `alembic -c
hub/alembic.ini upgrade head` first (see hub/DEPLOYMENT.md)."""

from __future__ import annotations

import contextlib

import uvicorn

from hub.config import HubConfig
from hub.db import make_engine, make_session_factory
from hub.observability import configure_logging
from hub.server import build_app


def build_server_app():
    """Build the ASGI app + a lifespan that disposes the engine on shutdown.

    Without the dispose, pooled Postgres connections are dropped rather than
    closed when the process exits, leaving the server to reap them on its own
    timeout -- which, on a rolling deploy that restarts every replica, can
    hold connections against max_connections long enough to lock out the new
    instances.
    """
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

    # Starlette composes an existing router lifespan with ours; the MCP app
    # already installs one (its session manager), so chain rather than
    # replace it.
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
    # log_config=None: uvicorn would otherwise install its own handlers and
    # undo the JSON formatting configure_logging() just set up.
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_config=None,
        # RequestContextMiddleware already logs every request (method, path,
        # status, duration, request id). uvicorn's own access line would add
        # a second copy WITH the query string -- a customer's memory search
        # text (?q=), a share link's token (?share_url=) -- through the same
        # root JSON handler.
        access_log=False,
        timeout_graceful_shutdown=config.graceful_shutdown_seconds,
    )


if __name__ == "__main__":
    main()
