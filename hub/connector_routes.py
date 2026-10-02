from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from hub.abuse import make_named_limiter, rate_limit_key
from hub.config import HubConfig
from hub.connectors import service

logger = logging.getLogger("commontrace.hub.connectors")

CONNECTOR_EVENTS_PATH = "/connectors/{connector_id}/events"


def add_connector_routes(app, session_factory, *, config: HubConfig, trusted_proxy_hops: int = 0) -> None:
    limiter = make_named_limiter(
        config, config.auth_attempts_per_minute, config.auth_attempts_burst, "connector_auth"
    )
    cipher = config.cipher()

    async def events(request: Request) -> Response:
        allowed, retry_after = await limiter.check(rate_limit_key(request, trusted_proxy_hops))
        if not allowed:
            return JSONResponse(
                {"error": "rate_limited"}, status_code=429,
                headers={"Retry-After": str(max(1, int(retry_after) + 1))},
            )
        body = await request.body()
        result = await service.ingest(
            session_factory, request.path_params["connector_id"], request.headers, body,
            cipher=cipher,
        )
        return JSONResponse(result.body, status_code=result.status)

    app.add_route(CONNECTOR_EVENTS_PATH, events, methods=["POST"])
