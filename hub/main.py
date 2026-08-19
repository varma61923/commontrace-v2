"""Run the Hub server: `python -m hub.main` (or the `commontrace-hub`
console script once the package is installed with the `[hub]` extra).

Reads all configuration from the environment (see hub/.env.example) via
hub/config.py. Does not run migrations itself -- run `alembic -c
hub/alembic.ini upgrade head` first (see hub/README.md)."""

from __future__ import annotations

import logging

import uvicorn

from hub.config import HubConfig
from hub.db import make_engine, make_session_factory
from hub.server import build_app


def main() -> None:
    config = HubConfig.from_env()
    logging.basicConfig(level=config.log_level)

    engine = make_engine(config)
    session_factory = make_session_factory(engine)
    app = build_app(config, session_factory)

    uvicorn.run(app, host=config.host, port=config.port, log_level=config.log_level.lower())


if __name__ == "__main__":
    main()
