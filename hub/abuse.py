"""Abuse controls for contribute_trace."""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from commontrace import memory_guard
from hub.config import HubConfig

logger = logging.getLogger("commontrace.hub.abuse")

_URL_RE = re.compile(r"https?://", re.IGNORECASE)


class TraceRejected(ValueError):
    """Hard rejection: the trace was not stored at all."""


class RateLimited(Exception):
    """Hard rejection: the org is over its contribute_trace rate limit."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def reject_unstorable_text(value: str, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string, got {type(value).__name__}")
    if "\x00" in value:
        raise ValueError(f"{field} contains an embedded NUL byte, which Postgres cannot store")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{field} contains a character that cannot be encoded as UTF-8 ({exc}); "
            "this usually means an unpaired UTF-16 surrogate reached this field as text"
        ) from exc


def validate_size(fields: dict, config: HubConfig) -> None:
    title = fields.get("title", "")
    context_text = fields.get("context_text", "")
    solution_text = fields.get("solution_text", "")
    tags = fields.get("tags") or []
    agent_type = fields.get("agent_type") or ""
    profile = fields.get("profile") or ""

    for value, name in (
        (title, "title"),
        (context_text, "context_text"),
        (solution_text, "solution_text"),
        (agent_type, "agent_type"),
        (profile, "profile"),
    ):
        reject_unstorable_text(value, name)
    for tag in tags:
        reject_unstorable_text(tag, "tag")

    if len(title) > config.max_title_chars:
        raise TraceRejected(f"title exceeds {config.max_title_chars} chars ({len(title)})")
    if len(context_text) > config.max_text_chars:
        raise TraceRejected(f"context_text exceeds {config.max_text_chars} chars ({len(context_text)})")
    if len(solution_text) > config.max_text_chars:
        raise TraceRejected(f"solution_text exceeds {config.max_text_chars} chars ({len(solution_text)})")
    if len(tags) > config.max_tags:
        raise TraceRejected(f"more than {config.max_tags} tags ({len(tags)})")
    for tag in tags:
        if len(tag) > config.max_tag_chars:
            raise TraceRejected(f"tag {tag!r} exceeds {config.max_tag_chars} chars")
    if len(profile) > 128:
        raise TraceRejected(f"profile exceeds 128 chars ({len(profile)})")

    serialized_size = len(json.dumps(fields, ensure_ascii=False).encode("utf-8"))
    if serialized_size > config.max_trace_bytes:
        raise TraceRejected(f"trace exceeds {config.max_trace_bytes} bytes serialized ({serialized_size})")


def suspicion_reason(fields: dict, config: HubConfig) -> str | None:
    text = " ".join(str(fields.get(k, "")) for k in ("title", "context_text", "solution_text"))

    url_count = len(_URL_RE.findall(text))
    if url_count > config.suspect_url_threshold:
        return f"contains {url_count} URLs (> {config.suspect_url_threshold} threshold)"

    stripped = text.strip()
    if stripped and len(set(stripped.lower())) <= 3 and len(stripped) > 20:
        return "content has near-zero character diversity (likely filler/spam)"

    guard = memory_guard.scan_fields({
        k: fields.get(k, "") for k in ("title", "context_text", "solution_text")
    })
    if guard.should_block:
        return guard.summary()

    return None


def resolve_client_key(request, trusted_proxy_hops: int) -> str:
    if trusted_proxy_hops <= 0:
        return request.client.host if request.client else "unknown"
    getlist = getattr(request.headers, "getlist", None)
    lines = getlist("x-forwarded-for") if getlist else [request.headers.get("x-forwarded-for", "")]
    hops = [h.strip() for line in lines for h in line.split(",") if h.strip()]
    if len(hops) >= trusted_proxy_hops:
        return _without_port(hops[-trusted_proxy_hops])
    return request.client.host if request.client else "unknown"


def _without_port(hop: str) -> str:
    if hop.startswith("["):
        end = hop.find("]")
        return hop[1:end] if end > 0 else hop
    if hop.count(":") == 1:
        host, _, port = hop.partition(":")
        if port.isdigit():
            return host
    return hop


def rate_limit_key(request, trusted_proxy_hops: int) -> str:
    key = resolve_client_key(request, trusted_proxy_hops)
    try:
        ip = ipaddress.ip_address(key)
    except ValueError:
        return key
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((ip, 64), strict=False))
    return key


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    _IDLE_TTL_SECONDS = 3600.0
    _SWEEP_INTERVAL_SECONDS = 300.0
    _MAX_TRACKED_KEYS = 100_000
    _DENY_ALL_RETRY_AFTER_SECONDS = 3600

    def __init__(self, per_minute: int, burst: int):
        self._rate_per_sec = per_minute / 60.0
        self._capacity = 0 if per_minute <= 0 else max(burst, 1)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    async def allow(self, key: str) -> bool:
        return (await self.check(key))[0]

    async def check(self, key: str) -> tuple[bool, float]:
        """(allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self._capacity), last_refill=now)
                self._evict_if_over_capacity(now)
                self._buckets[key] = bucket
            elapsed = now - bucket.last_refill
            bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._rate_per_sec)
            bucket.last_refill = now
            if now - self._last_sweep >= self._SWEEP_INTERVAL_SECONDS:
                self._sweep_idle_buckets(now)
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0
            if self._rate_per_sec <= 0:
                return False, float(self._DENY_ALL_RETRY_AFTER_SECONDS)
            return False, (1.0 - bucket.tokens) / self._rate_per_sec

    def _evict_if_over_capacity(self, now: float) -> None:
        if len(self._buckets) < self._MAX_TRACKED_KEYS:
            return
        self._sweep_idle_buckets(now)
        while len(self._buckets) >= self._MAX_TRACKED_KEYS:
            oldest = min(self._buckets, key=lambda k: self._buckets[k].last_refill)
            del self._buckets[oldest]

    def refund(self, key: str) -> None:
        """Give back one token, never exceeding capacity."""
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is not None:
                bucket.tokens = min(self._capacity, bucket.tokens + 1.0)

    def _sweep_idle_buckets(self, now: float) -> None:
        stale_keys = [
            key for key, bucket in self._buckets.items()
            if now - bucket.last_refill >= self._IDLE_TTL_SECONDS
        ]
        for key in stale_keys:
            del self._buckets[key]
        self._last_sweep = now


class RateLimiterBackend(Protocol):
    async def allow(self, key: str) -> bool: ...
    async def check(self, key: str) -> tuple[bool, float]: ...
    def refund(self, key: str) -> None: ...


def _to_asyncpg_dsn(database_url: str) -> str:
    prefix = "postgresql+asyncpg://"
    if database_url.startswith(prefix):
        return "postgresql://" + database_url[len(prefix):]
    return database_url


_RATE_LIMIT_TABLE_EXISTS_SQL = "SELECT to_regclass('hub_rate_limit_buckets') IS NOT NULL"

_RATE_LIMIT_DDL = """
    CREATE TABLE IF NOT EXISTS hub_rate_limit_buckets (
        limiter_name text NOT NULL,
        bucket_key text NOT NULL,
        tokens double precision NOT NULL,
        last_refill timestamptz NOT NULL,
        PRIMARY KEY (limiter_name, bucket_key)
    )
"""

_RATE_LIMIT_TAKE_SQL = """
    INSERT INTO hub_rate_limit_buckets AS b (limiter_name, bucket_key, tokens, last_refill)
    VALUES (
        $1, $2,
        CASE WHEN $3::float8 >= 1 THEN $3::float8 - 1 ELSE $3::float8 END,
        CASE WHEN $3::float8 >= 1 THEN now() ELSE now() + interval '1 microsecond' END
    )
    ON CONFLICT (limiter_name, bucket_key) DO UPDATE
    SET tokens = CASE
            WHEN LEAST($3::float8, b.tokens + EXTRACT(EPOCH FROM (now() - b.last_refill)) * $4::float8) >= 1
            THEN LEAST($3::float8, b.tokens + EXTRACT(EPOCH FROM (now() - b.last_refill)) * $4::float8) - 1
            ELSE LEAST($3::float8, b.tokens + EXTRACT(EPOCH FROM (now() - b.last_refill)) * $4::float8)
        END,
        last_refill = CASE
            WHEN LEAST($3::float8, b.tokens + EXTRACT(EPOCH FROM (now() - b.last_refill)) * $4::float8) >= 1
            THEN now()
            ELSE now() + interval '1 microsecond'
        END
    RETURNING tokens, last_refill = now() AS allowed
"""

_RATE_LIMIT_SWEEP_SQL = """
    DELETE FROM hub_rate_limit_buckets
    WHERE limiter_name = $1 AND last_refill < now() - ($2 * interval '1 second')
"""

_RATE_LIMIT_REFUND_SQL = """
    UPDATE hub_rate_limit_buckets SET tokens = LEAST($3, tokens + 1)
    WHERE limiter_name = $1 AND bucket_key = $2
"""

_POOL_STARTUP_TIMEOUT_SECONDS = 30.0

_POOL_CALL_TIMEOUT_SECONDS = 10.0


class _SharedPgPool:
    _POOL_MIN_SIZE = 1
    _POOL_MAX_SIZE = 5

    def __init__(self, dsn: str):
        self.loop = asyncio.new_event_loop()
        self.pool = None
        self._init_error: BaseException | None = None
        self._stop_event = threading.Event()
        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(dsn, ready), name="hub-ratelimit-pg", daemon=True
        )
        self._thread.start()
        if not ready.wait(timeout=_POOL_STARTUP_TIMEOUT_SECONDS):
            self._stop_event.set()
            def _cancel_and_stop():
                for task in asyncio.all_tasks(self.loop):
                    task.cancel()
                self.loop.stop()
            try:
                self.loop.call_soon_threadsafe(_cancel_and_stop)
            except RuntimeError:
                pass
            raise TimeoutError(
                f"timed out after {_POOL_STARTUP_TIMEOUT_SECONDS}s waiting for the "
                "HUB_RATE_LIMIT_BACKEND=postgres connection pool to start"
            )
        if self._init_error is not None:
            raise self._init_error

    def _run(self, dsn: str, ready: threading.Event) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            import asyncpg

            self.pool = self.loop.run_until_complete(self._setup(asyncpg, dsn))
        except BaseException as exc:  # noqa: BLE001 - surfaced to __init__ via self._init_error
            self._init_error = exc
            ready.set()
            if not self.loop.is_closed():
                self.loop.close()
            return
        if self._stop_event.is_set():
            if self.pool is not None:
                try:
                    self.loop.run_until_complete(self.pool.close())
                except Exception:
                    pass
            if not self.loop.is_closed():
                self.loop.close()
            return
        ready.set()
        try:
            self.loop.run_forever()
        finally:
            if not self.loop.is_closed():
                self.loop.close()

    async def _setup(self, asyncpg, dsn: str):
        pool = await asyncpg.create_pool(dsn, min_size=self._POOL_MIN_SIZE, max_size=self._POOL_MAX_SIZE)
        try:
            async with pool.acquire() as conn:
                if not await conn.fetchval(_RATE_LIMIT_TABLE_EXISTS_SQL):
                    await conn.execute(_RATE_LIMIT_DDL)
        except asyncpg.exceptions.DuplicateTableError:
            pass
        return pool


_shared_pools: dict[str, _SharedPgPool] = {}
_shared_pools_lock = threading.Lock()


def _get_shared_pool(dsn: str) -> _SharedPgPool:
    with _shared_pools_lock:
        pool = _shared_pools.get(dsn)
        if pool is None:
            pool = _SharedPgPool(dsn)
            _shared_pools[dsn] = pool
        return pool


class PostgresRateLimiter:
    _IDLE_TTL_SECONDS = RateLimiter._IDLE_TTL_SECONDS
    _SWEEP_INTERVAL_SECONDS = RateLimiter._SWEEP_INTERVAL_SECONDS

    def __init__(self, per_minute: int, burst: int, database_url: str, limiter_name: str):
        self._rate_per_sec = per_minute / 60.0
        self._capacity = 0.0 if per_minute <= 0 else float(max(burst, 1))
        self._limiter_name = limiter_name
        self._last_sweep = time.monotonic()
        self._shared = _get_shared_pool(_to_asyncpg_dsn(database_url))

    async def _await_on_pool_loop(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self._shared.loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=_POOL_CALL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            future.cancel()
            raise

    async def allow(self, key: str) -> bool:
        return await self._await_on_pool_loop(self._allow_async(key))

    async def _allow_async(self, key: str) -> bool:
        allowed, _retry_after = await self._refill_and_maybe_decrement(key)
        return allowed

    async def check(self, key: str) -> tuple[bool, float]:
        return await self._await_on_pool_loop(self._check_async(key))

    async def _check_async(self, key: str) -> tuple[bool, float]:
        allowed, tokens = await self._refill_and_maybe_decrement(key)
        if allowed:
            return True, 0.0
        if self._rate_per_sec <= 0:
            return False, float(RateLimiter._DENY_ALL_RETRY_AFTER_SECONDS)
        return False, (1.0 - tokens) / self._rate_per_sec

    async def _refill_and_maybe_decrement(self, key: str) -> tuple[bool, float]:
        async with self._shared.pool.acquire() as conn:
            row = await conn.fetchrow(
                _RATE_LIMIT_TAKE_SQL, self._limiter_name, key, self._capacity, self._rate_per_sec
            )
        self._maybe_sweep()
        return bool(row["allowed"]), float(row["tokens"])

    def refund(self, key: str) -> None:
        future = asyncio.run_coroutine_threadsafe(self._refund_async(key), self._shared.loop)
        future.add_done_callback(self._log_refund_failure)

    async def _refund_async(self, key: str) -> None:
        await self._shared.pool.execute(_RATE_LIMIT_REFUND_SQL, self._limiter_name, key, self._capacity)

    @staticmethod
    def _log_refund_failure(future: concurrent.futures.Future) -> None:
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            logger.warning("hub_rate_limit_buckets refund failed: %r", exc)

    def _maybe_sweep(self) -> None:
        now = time.monotonic()
        if now - self._last_sweep < self._SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        task = self._shared.loop.create_task(self._sweep_idle_rows())
        task.add_done_callback(self._log_sweep_failure)

    @staticmethod
    def _log_sweep_failure(task: asyncio.Task) -> None:
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            logger.warning("hub_rate_limit_buckets sweep failed: %r", exc)

    async def _sweep_idle_rows(self) -> None:
        await self._shared.pool.execute(_RATE_LIMIT_SWEEP_SQL, self._limiter_name, self._IDLE_TTL_SECONDS)

    def close(self) -> None:
        ...

    def __repr__(self) -> str:
        return f"PostgresRateLimiter(limiter_name={self._limiter_name!r}, capacity={self._capacity!r})"


def make_named_limiter(
    config: HubConfig | None, per_minute: int, burst: int, limiter_name: str
) -> RateLimiterBackend:
    """A limiter on the deployment's configured backend, under its own name."""
    if config is not None and config.rate_limit_backend == "postgres":
        return PostgresRateLimiter(
            per_minute=per_minute, burst=burst, database_url=config.database_url, limiter_name=limiter_name
        )
    return RateLimiter(per_minute=per_minute, burst=burst)


def make_rate_limiter(config: HubConfig) -> RateLimiterBackend:
    return make_named_limiter(config, config.rate_limit_per_minute, config.rate_limit_burst, "write")


def make_read_rate_limiter(config: HubConfig) -> RateLimiterBackend:
    return make_named_limiter(config, config.read_rate_limit_per_minute, config.read_rate_limit_burst, "read")


def make_auth_rate_limiter(config: HubConfig) -> RateLimiterBackend:
    return make_named_limiter(config, config.auth_attempts_per_minute, config.auth_attempts_burst, "auth")


def make_scim_auth_rate_limiter(config: HubConfig) -> RateLimiterBackend:
    return make_named_limiter(config, config.auth_attempts_per_minute, config.auth_attempts_burst, "scim_auth")
