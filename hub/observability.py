"""Structured logging, request correlation, and liveness/readiness probes."""

from __future__ import annotations

import contextvars
import json
import logging
import math
import re
import threading
import time
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from hub.abuse import RateLimiter, make_named_limiter, rate_limit_key

REQUEST_ID_HEADER = "X-Request-ID"

current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_request_id", default=None
)

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
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())


_MAX_CLIENT_REQUEST_ID_LEN = 128
_VALID_CLIENT_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,%d}$" % _MAX_CLIENT_REQUEST_ID_LEN)


def _resolve_request_id(client_supplied: str | None) -> str:
    if client_supplied and _VALID_CLIENT_REQUEST_ID.match(client_supplied):
        return client_supplied
    return str(uuid.uuid4())


class RequestContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, logger_name: str = "commontrace.hub.request"):
        super().__init__(app)
        self._logger = logging.getLogger(logger_name)

    async def dispatch(self, request: Request, call_next):
        request_id = _resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        token = current_request_id.set(request_id)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            METRICS.observe_request(request.method, request.url.path, status, duration_ms)
            if status == 429:
                METRICS.observe_rate_limited("http")
            self._logger.info(
                "request",
                extra={
                    "http_method": request.method,
                    "http_path": loggable_path(request.url.path),
                    "http_status": status,
                    "duration_ms": duration_ms,
                },
            )
            current_request_id.reset(token)


_SECRET_PATH_SEGMENT = re.compile(r"(/proof/shared/)[^/]+")


def loggable_path(path: str) -> str:
    """`path` with any secret-bearing segment replaced by `[redacted]`."""
    return _SECRET_PATH_SEGMENT.sub(r"\1[redacted]", path)


class Metrics:
    """Process-wide counters, rendered in Prometheus text format."""

    BUCKETS_MS: tuple[float, ...] = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, int], int] = {}
        self._duration_buckets: dict[str, list[int]] = {}
        self._duration_sum_ms: dict[str, float] = {}
        self._duration_count: dict[str, int] = {}
        self._rate_limited: dict[str, int] = {}

    def observe_request(self, method: str, path: str, status: int, duration_ms: float) -> None:
        method_bucket = method if method in _KNOWN_METHODS else "OTHER"
        bucket = path if path in _KNOWN_PATHS else "other"
        with self._lock:
            key = (method_bucket, bucket, status)
            self._requests[key] = self._requests.get(key, 0) + 1
            self._duration_sum_ms[bucket] = self._duration_sum_ms.get(bucket, 0.0) + duration_ms
            self._duration_count[bucket] = self._duration_count.get(bucket, 0) + 1
            counts = self._duration_buckets.setdefault(bucket, [0] * (len(self.BUCKETS_MS) + 1))
            for i, upper in enumerate(self.BUCKETS_MS):
                if duration_ms <= upper:
                    counts[i] += 1
            counts[-1] += 1

    def observe_rate_limited(self, limiter: str) -> None:
        with self._lock:
            self._rate_limited[limiter] = self._rate_limited.get(limiter, 0) + 1

    def render(self) -> str:
        with self._lock:
            requests = dict(self._requests)
            duration_buckets = {k: list(v) for k, v in self._duration_buckets.items()}
            duration_sum = dict(self._duration_sum_ms)
            duration_count = dict(self._duration_count)
            rate_limited = dict(self._rate_limited)

        lines = [
            "# HELP commontrace_hub_requests_total Requests served, by method, route and status.",
            "# TYPE commontrace_hub_requests_total counter",
        ]
        for (method, path, status), count in sorted(requests.items()):
            lines.append(
                f'commontrace_hub_requests_total{{method="{method}",path="{path}",'
                f'status="{status}"}} {count}'
            )
        lines += [
            "# HELP commontrace_hub_request_duration_ms Request duration in milliseconds, by route.",
            "# TYPE commontrace_hub_request_duration_ms histogram",
        ]
        for path in sorted(duration_buckets):
            counts = duration_buckets[path]
            for upper, cumulative in zip(self.BUCKETS_MS, counts):
                lines.append(
                    f'commontrace_hub_request_duration_ms_bucket{{path="{path}",le="{upper:g}"}} {cumulative}'
                )
            lines.append(
                f'commontrace_hub_request_duration_ms_bucket{{path="{path}",le="+Inf"}} {counts[-1]}'
            )
            lines.append(
                f'commontrace_hub_request_duration_ms_sum{{path="{path}"}} {duration_sum[path]:.2f}'
            )
            lines.append(
                f'commontrace_hub_request_duration_ms_count{{path="{path}"}} {duration_count[path]}'
            )
        lines += [
            "# HELP commontrace_hub_rate_limited_total Requests refused, by which limiter refused them.",
            "# TYPE commontrace_hub_rate_limited_total counter",
        ]
        for limiter, count in sorted(rate_limited.items()):
            lines.append(f'commontrace_hub_rate_limited_total{{limiter="{limiter}"}} {count}')
        return "\n".join(lines) + "\n"


_KNOWN_PATHS = frozenset({"/mcp", "/healthz", "/readyz", "/metrics"})

_KNOWN_METHODS = frozenset({"GET", "POST", "DELETE", "HEAD", "OPTIONS", "PUT", "PATCH"})

METRICS = Metrics()


def add_health_routes(
    app,
    session_factory: async_sessionmaker,
    readyz_rate_limiter: RateLimiter | None = None,
    trusted_proxy_hops: int = 0,
    metrics_token: str = "",
) -> None:
    readyz_rate_limiter = readyz_rate_limiter or make_named_limiter(None, 120, 30, "readyz")

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def readyz(request: Request) -> JSONResponse:
        client_key = rate_limit_key(request, trusted_proxy_hops) if request is not None else "unknown"
        allowed, retry_after = await readyz_rate_limiter.check(client_key)
        if not allowed:
            seconds = str(max(1, math.ceil(retry_after)))
            return JSONResponse(
                {"status": "rate_limited", "retry_after": int(seconds)},
                status_code=429,
                headers={"Retry-After": seconds},
            )
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
            logging.getLogger("commontrace.hub").warning(
                "readiness check failed", extra={"error": str(exc)}
            )
            return JSONResponse({"status": "not_ready", "database": "unreachable"}, status_code=503)
        return JSONResponse({"status": "ready", "database": "ok"})

    async def metrics(request: Request) -> Response:
        client_key = rate_limit_key(request, trusted_proxy_hops) if request is not None else "unknown"
        allowed, retry_after = await readyz_rate_limiter.check(client_key)
        if not allowed:
            seconds = str(max(1, math.ceil(retry_after)))
            return JSONResponse(
                {"status": "rate_limited", "retry_after": int(seconds)},
                status_code=429,
                headers={"Retry-After": seconds},
            )
        if metrics_token:
            import hmac

            header = request.headers.get("authorization", "") if request is not None else ""
            scheme, _, presented = header.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                presented.strip().encode(), metrics_token.encode()
            ):
                return JSONResponse(
                    {"status": "unauthorized"}, status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="metrics"'},
                )
        return Response(METRICS.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    app.add_route("/healthz", healthz, methods=["GET"])
    app.add_route("/readyz", readyz, methods=["GET"])
    app.add_route("/metrics", metrics, methods=["GET"])
