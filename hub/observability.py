"""Structured logging, request correlation, and liveness/readiness probes.

Three things a deployment needs that plain `logging.basicConfig` doesn't
give you:

1. **JSON logs.** Human-formatted lines are unqueryable once they're in a
   log aggregator. Every record goes out as one JSON object.

2. **A request id on every line.** Without correlation you cannot tell
   which log lines belong to the request that failed. A contextvar carries
   an id (from the client's `X-Request-ID` when present, else generated)
   into every record emitted while handling that request.

3. **Liveness vs. readiness as separate questions.** `/healthz` previously
   returned 200 unconditionally -- including when Postgres was unreachable
   -- so a load balancer would happily keep routing traffic to an instance
   that could not serve a single request. They are now split:
     - `/healthz` (liveness): "is this process alive?" No dependency checks.
       A failing liveness probe means *restart me*, so it must NOT depend on
       the database -- otherwise a brief DB blip restarts every replica at
       once and turns an outage into a worse outage.
     - `/readyz` (readiness): "should I receive traffic?" Actually executes
       `SELECT 1`. A failing readiness probe means *stop routing to me*,
       which is the correct response to a DB that's down.

Logging never includes an API key: the auth middleware puts only the
non-secret key prefix into context (see hub/auth.py).
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

REQUEST_ID_HEADER = "X-Request-ID"

current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_request_id", default=None
)

# Attributes present on every LogRecord; anything else a caller attached via
# `extra=` is merged into the JSON output as a custom field.
_STANDARD_LOGRECORD_ATTRS = frozenset(
    """
    args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName
    relativeCreated stack_info thread threadName taskName
    """.split()
)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = current_request_id.get()
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOGRECORD_ATTRS and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Replace the root handler with a single JSON-emitting one. Idempotent:
    safe to call more than once (a second call won't stack handlers)."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns/propagates a request id, logs one structured line per request
    with its outcome and duration, and echoes the id back in the response so
    a client can quote it in a bug report."""

    def __init__(self, app, logger_name: str = "commontrace.hub.request"):
        super().__init__(app)
        self._logger = logging.getLogger(logger_name)

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        token = current_request_id.set(request_id)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            # Deliberately does not log query strings or bodies: request
            # payloads here carry customer trace content.
            self._logger.info(
                "request",
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "http_status": status,
                    "duration_ms": duration_ms,
                },
            )
            current_request_id.reset(token)


def add_health_routes(app, session_factory: async_sessionmaker) -> None:
    """Wire /healthz (liveness) and /readyz (readiness). See module docstring
    for why these must answer different questions."""

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def readyz(request: Request) -> JSONResponse:
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
            logging.getLogger("commontrace.hub").warning(
                "readiness check failed", extra={"error": str(exc)}
            )
            return JSONResponse({"status": "not_ready", "database": "unreachable"}, status_code=503)
        return JSONResponse({"status": "ready", "database": "ok"})

    app.add_route("/healthz", healthz, methods=["GET"])
    app.add_route("/readyz", readyz, methods=["GET"])
