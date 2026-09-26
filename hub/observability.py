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

from hub.abuse import RateLimiter, rate_limit_key

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


# A client-supplied X-Request-ID is echoed into every log line for this
# request and back into the response header verbatim -- accepting it
# unbounded would let a client put an arbitrarily long or arbitrarily
# encoded value into both. The JSON log encoding already prevents newline
# log-forging (control characters get escaped inside a string field, not
# emitted as raw line breaks), but bounding length and character set here
# is still cheap defense-in-depth against oversized values and anything
# that could misbehave in a raw (non-JSON) log consumer or a header value.
_MAX_CLIENT_REQUEST_ID_LEN = 128
_VALID_CLIENT_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,%d}$" % _MAX_CLIENT_REQUEST_ID_LEN)


def _resolve_request_id(client_supplied: str | None) -> str:
    if client_supplied and _VALID_CLIENT_REQUEST_ID.match(client_supplied):
        return client_supplied
    return str(uuid.uuid4())


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns/propagates a request id, logs one structured line per request
    with its outcome and duration, and echoes the id back in the response so
    a client can quote it in a bug report."""

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
            # Cheap, always-safe defense-in-depth headers on every response.
            # The HTML pages (console, admin, signup) send their own
            # Content-Security-Policy, hashed to their own inline scripts
            # (hub/admin.py:html_headers); these cost nothing and remove a
            # browser's default assumptions that don't hold for a JSON API
            # either: don't guess the content type from the body
            # (nosniff), never render a response in a frame, don't leak the
            # request URL to a Referer header on outbound links from any
            # tool that happens to render this JSON. HSTS is a no-op unless
            # the response is actually delivered over TLS (browsers ignore
            # it on plain HTTP per spec), so it is harmless to set
            # unconditionally rather than trying to detect the upstream
            # proxy's scheme from a spoofable X-Forwarded-Proto header.
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
            # Deliberately does not log query strings or bodies: request
            # payloads here carry customer trace content.
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


# A share link's token IS the credential for that report (anyone holding
# the URL sees the org's live data for its lifetime), so it never reaches a
# log line: log shippers and aggregators are read far more widely than the
# console is.
_SECRET_PATH_SEGMENT = re.compile(r"(/proof/shared/)[^/]+")


def loggable_path(path: str) -> str:
    """`path` with any secret-bearing segment replaced by `[redacted]`."""
    return _SECRET_PATH_SEGMENT.sub(r"\1[redacted]", path)


class Metrics:
    """Process-wide counters, rendered in Prometheus text format.

    WHY: nothing in this Hub could answer "how many requests are we
    refusing, and why" without grepping JSON logs after the fact. Rate
    limiting in particular is invisible until a customer complains -- the
    429s are the earliest signal that a limit is set wrong or that a client
    is misbehaving, and they were only ever a log line.

    WHAT IS DELIBERATELY NOT LABELLED: org_id, api key prefix, tool
    arguments, query text. Per-org labels would make this a cardinality
    problem (one time series per customer, unbounded) and a privacy one --
    a scrape endpoint is a different trust boundary from an authenticated
    tool call, and DATA_RETENTION.md's reasoning about not logging query
    text applies here for the same reason. Method, path and status are
    bounded, non-identifying sets.

    Counters only, never gauges derived from the database: this must stay a
    cheap in-memory read, so that scraping it can never itself become load
    on Postgres the way /readyz can.

    Duration is a proper Prometheus histogram (bucketed counts + _sum +
    _count), not the summed-duration-only counter this used to be. A sum
    alone cannot answer "is p99 acceptable" -- a stated SLO needs a
    percentile, and a percentile needs a distribution, which a single
    running total structurally cannot recover no matter how it is sliced.
    Bucketed the same way `hub/bench_scaling.py`/`bench_retrieval.py`
    report their own numbers is not required here; this is live production
    traffic, not a benchmark run, so PromQL's own `histogram_quantile()` is
    what turns these buckets into a percentile at query time.
    """

    # Upper bounds in milliseconds, Prometheus's own `le` (less-or-equal)
    # convention: BUCKETS_MS[i] counts every observation <= that value,
    # cumulatively, so the last bucket before +Inf already holds "everything
    # this fast or faster". Skewed toward the sub-100ms range because that
    # is where this Hub's own budget lives -- hub/auth.py's fast path is
    # ~1ms, hub/SCALING.md's sublinear read paths top out around 60-100ms at
    # 64k traces -- with enough coarse buckets past 1s to still say something
    # about a genuinely slow outlier instead of just lumping it into +Inf.
    BUCKETS_MS: tuple[float, ...] = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, int], int] = {}
        # path -> [count landing in BUCKETS_MS[0], ..., count in +Inf].
        # One extra slot for +Inf beyond BUCKETS_MS's own length, matching
        # Prometheus's requirement that the last bucket is always +Inf.
        self._duration_buckets: dict[str, list[int]] = {}
        self._duration_sum_ms: dict[str, float] = {}
        self._duration_count: dict[str, int] = {}
        self._rate_limited: dict[str, int] = {}

    def observe_request(self, method: str, path: str, status: int, duration_ms: float) -> None:
        # The path is bucketed to the routes this app actually serves, so a
        # client cannot inflate cardinality by requesting /aaaa, /aaab, ...
        # -- an unbounded label set is the classic way a metrics endpoint
        # becomes the outage. `method` needs the identical treatment: an
        # HTTP method is only constrained by RFC 7230's `token` grammar, so
        # an unauthenticated caller hitting any nonexistent path (a cheap
        # 404, gated by no rate limiter -- ApiKeyAuthMiddleware only meters
        # the MCP path) could otherwise grow this process-lifetime,
        # never-evicted dict without bound just by varying the verb string.
        method_bucket = method if method in _KNOWN_METHODS else "OTHER"
        bucket = path if path in _KNOWN_PATHS else "other"
        with self._lock:
            key = (method_bucket, bucket, status)
            self._requests[key] = self._requests.get(key, 0) + 1
            self._duration_sum_ms[bucket] = self._duration_sum_ms.get(bucket, 0.0) + duration_ms
            self._duration_count[bucket] = self._duration_count.get(bucket, 0) + 1
            counts = self._duration_buckets.setdefault(bucket, [0] * (len(self.BUCKETS_MS) + 1))
            # Every bucket this observation is <= gets incremented, not just
            # the tightest one -- that IS what "cumulative" means in a
            # Prometheus histogram, and is what lets a query pick any `le`
            # threshold after the fact without this class having guessed
            # which ones an operator would care about.
            for i, upper in enumerate(self.BUCKETS_MS):
                if duration_ms <= upper:
                    counts[i] += 1
            counts[-1] += 1  # +Inf: every observation, unconditionally

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

# The standard HTTP methods any route on this app could plausibly receive
# from a real client (GET/POST for most routes, DELETE for MCP session
# teardown, HEAD/OPTIONS/PUT/PATCH from a generic client or proxy probing
# capabilities) -- not an allowlist of what's actually wired to a handler,
# just the bound on the label set `path` already gets for the identical
# cardinality reason.
_KNOWN_METHODS = frozenset({"GET", "POST", "DELETE", "HEAD", "OPTIONS", "PUT", "PATCH"})

# One process-wide instance: middleware and route handlers are constructed at
# different points in build_app, and threading a shared object through both
# adds a parameter to every one of them for no benefit over a module-level
# counter set, which is what a metrics registry is in every library that
# implements one.
METRICS = Metrics()


def add_health_routes(
    app,
    session_factory: async_sessionmaker,
    readyz_rate_limiter: RateLimiter | None = None,
    trusted_proxy_hops: int = 0,
    metrics_token: str = "",
) -> None:
    """Wire /healthz (liveness) and /readyz (readiness). See module docstring
    for why these must answer different questions.

    Both are unauthenticated by design -- an orchestrator's liveness/
    readiness prober does not carry a tenant API key -- which is exactly
    what makes /readyz a DoS lever ApiKeyAuthMiddleware's own rate limiters
    never see: it executes a real `SELECT 1` against the database pool on
    every call, so flooding it (unlike /healthz, which touches nothing)
    can exhaust connections the same way any other unbounded query would.
    `readyz_rate_limiter` is keyed by client address and defaults to a
    generous bucket that a real orchestrator's poll interval (typically
    every few seconds) never comes close to. `trusted_proxy_hops` is
    HubConfig.trusted_proxy_hops, passed straight through to
    hub.abuse.rate_limit_key -- see that config field's docstring."""
    readyz_rate_limiter = readyz_rate_limiter or RateLimiter(per_minute=120, burst=30)

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def readyz(request: Request) -> JSONResponse:
        client_key = rate_limit_key(request, trusted_proxy_hops) if request is not None else "unknown"
        allowed, retry_after = await readyz_rate_limiter.check(client_key)
        if not allowed:
            # Retry-After for the same reason hub/server.py's 429s carry it:
            # an orchestrator that backs off by a known interval stops
            # adding load to a probe endpoint that is already refusing.
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
        """Prometheus scrape endpoint. Unauthenticated, like the probes, and
        for the same reason: a scraper carries no tenant credential.

        That is safe here only because of what Metrics deliberately does NOT
        record -- no org ids, no key prefixes, no query text (see its
        docstring). Expose it to your monitoring network, not the public
        internet, the same way you would any /metrics; the Hub's documented
        deployment already puts a reverse proxy in front (hub/DEPLOYMENT.md).

        Rate limited on the same bucket as /readyz so an unauthenticated
        caller cannot use it as a free request generator; unlike /readyz it
        touches no database, so it is cheap even when the database is down
        -- which is exactly when you want to be able to read it.
        """
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
            # Optional (HUB_METRICS_TOKEN): a deployment that cannot keep
            # /metrics off a reachable network can require the scraper's
            # bearer token. Compared in constant time.
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
