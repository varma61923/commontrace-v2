"""Abuse controls for contribute_trace.

contribute_trace writes into the calling org's own private corpus only --
it can never reach the CommonTrace Knowledge Base, which only
hub/manage.py:commons_seed writes to -- so a careless or malicious
contributor can only degrade its own org's retrieval quality, never
another org's. The controls below exist anyway: to protect the shared
Postgres instance every org's writes land on, and to keep an org's own
search useful for that org. contribute_trace runs through, in order:

  1. schema validation (hub/schema_validation.py)      -> hard reject, 4xx
  2. per-field / per-trace size limits (this module)     -> hard reject, 4xx
  3. per-org rate limiting (this module)                 -> hard reject, 429
  4. a cheap suspicion heuristic, now including a content-safety scan for
     secrets and prompt-injection payloads (commontrace/memory_guard.py,
     OWASP ASI06) -> soft: store quarantined=True, excluded from
     search_traces, pending manual review

Rate limiting keys off org_id and, by default, is an in-memory token bucket
(`RateLimiter` below). That is a known, documented MVP limitation: it resets
on process restart and does not coordinate across multiple server instances
-- a horizontally-scaled deployment sees roughly N replicas x the configured
rate. `PostgresRateLimiter` (also below) is an opt-in second backend that
shares bucket state in a table in the Hub's own Postgres database instead,
so replicas enforce one real shared limit. Select it with
HUB_RATE_LIMIT_BACKEND=postgres (hub/config.py); the default stays "memory"
so existing single-process deployments are unaffected. See hub/DEPLOYMENT.md
section 6 for the operational trade-offs of each.

Both backends expose the same `check(key) -> (bool, retry_after)` and
`refund(key) -> None` methods (plus a simpler `allow(key) -> bool`) --
callers (hub/server.py's ApiKeyAuthMiddleware, hub/crud.py's contribute_trace/
amend_trace/submit_kb_entry) call `check`/`refund` synchronously, un-awaited,
from inside async functions, so those signatures are load-bearing and neither
backend can change them. See RateLimiterBackend's docstring below.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
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
    """Hard rejection: the org is over its contribute_trace rate limit.

    Carries `retry_after` (seconds) so the refusal can tell a client when to
    come back, exactly like the HTTP 429s hub/server.py returns. Without it
    a bulk contributor can only guess -- and this limiter's shipped default
    (20/minute, burst 5) means a client pushing a backlog is refused as a
    matter of course, not as an error, so "when" is the only useful part of
    the answer.
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def reject_unstorable_text(value: str, field: str) -> None:
    """Raise `ValueError` if `value` contains an embedded NUL byte (0x00) or
    a lone UTF-16 surrogate (U+D800-U+DFFF unpaired) -- the two distinct
    ways a Python `str` can be perfectly legal to hold in memory and still
    be impossible for Postgres to store.

    Every free-text string this Hub accepts eventually becomes an asyncpg
    bind parameter. The two failure modes have different root causes but
    the same symptom, so one function checks for both:

    - A NUL byte is valid Unicode and encodes to valid UTF-8 (`b"\\x00"`)
      -- Postgres simply cannot store it, because its text type is built on
      NUL-terminated C strings on-disk. asyncpg raises
      `CharacterNotInRepertoireError` for this one.
    - A lone surrogate is not valid Unicode *text* at all -- surrogate code
      points exist only as UTF-16 encoding machinery and have no UTF-8
      representation, so `value.encode("utf-8")` itself raises. A client
      cannot send one deliberately as ordinary text, but `json.loads`
      happily decodes a malformed `"\\ud800"` escape (an unpaired
      surrogate half) into exactly this, so any JSON-speaking MCP client --
      buggy or adversarial -- can produce one without trying hard. asyncpg
      raises `DataError` for this one.

    Both, left unchecked, get raised from deep inside a query -- wrapped in
    a bare `DBAPIError`/`ProgrammingError` that matches none of
    `hub/server.py:_error_response`'s specific branches, so it falls
    through to a generic `internal_error` and a server-side stack trace
    logged as "unexpected" -- for input that is exactly as malformed, and
    exactly as foreseeable, as an over-length title. Checked here, before
    the value reaches a query, either one gets the same clean 4xx every
    other malformed-input case in this module already gets, instead of the
    one path that still reaches Postgres itself to fail.

    `ValueError` (not `TraceRejected`) is the return type because this same
    check is reused for read-path filters and non-trace fields that have no
    concept of "the trace was rejected"; `hub/server.py:_error_response`
    maps a bare `ValueError` to `invalid_request` the same as it does
    `TraceRejected`, which is a subclass of it. The surrogate branch below
    re-wraps `encode()`'s own `UnicodeEncodeError` (itself already a
    `ValueError` subclass, so it would reach the same 4xx unwrapped) purely
    so the message names the field, matching every other check in this
    module rather than surfacing a raw Python encoding error to a caller.

    The isinstance check below exists because at least one caller
    (search_traces's `for tag in tags or []: reject_unstorable_text(tag,
    "tag")`) has no schema validation ahead of it the way
    contribute_trace/amend_trace/submit_kb_entry's `validate_trace` does --
    those reject a non-string tag with a clean SchemaValidationError before
    this function ever runs, but search_traces is a read path with no
    schema to check against. Reproduced live: search_traces(tags=[123])
    reached `"\\x00" in value` with `value=123` and crashed with an
    uncaught `TypeError: argument of type 'int' is not iterable` -- not a
    ValueError, so it fell through to the same generic 500 every other fix
    in this function exists to prevent. This does not, on its own, catch a
    caller passing a bare string instead of a list of strings
    (search_traces(tags="abc") iterates the string's own characters, each
    one individually a valid str) -- that is a different failure mode, an
    array-vs-scalar mismatch at the SQL layer, guarded separately where the
    container itself is accepted.
    """
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
    # Not every caller's `fields` includes these -- amend_trace's wire dict
    # never set a new agent_type or profile -- so both are read the same
    # defensive way as `tags` above rather than assumed present.
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
    # Trace.profile is String(128) -- a literal bound, not a config knob,
    # like agent_id/idempotency_key's identical checks in crud.py: this is
    # the column's actual capacity, not a customer-facing quality setting.
    # Left to the INSERT, an over-long value raises
    # asyncpg.StringDataRightTruncation (a DataError, not an
    # IntegrityError), which surfaces as an opaque HTTP 500 instead of a
    # clean rejection.
    if len(profile) > 128:
        raise TraceRejected(f"profile exceeds 128 chars ({len(profile)})")

    serialized_size = len(json.dumps(fields, ensure_ascii=False).encode("utf-8"))
    if serialized_size > config.max_trace_bytes:
        raise TraceRejected(f"trace exceeds {config.max_trace_bytes} bytes serialized ({serialized_size})")


def suspicion_reason(fields: dict, config: HubConfig) -> str | None:
    """Return a short human-readable reason to quarantine this trace, or
    None if it looks fine. Deliberately simple and named as a placeholder:
    this is not a moderation system, just a first line of defense against
    obvious spam. Replace/extend as real abuse patterns are observed.

    Also runs `commontrace.memory_guard` over the same three fields --
    OWASP ASI06 (Memory & Context Poisoning): a trace stored here is
    fleet-wide history a later agent reads back, and a quarantined trace is
    excluded from `search_traces` (hub/crud.py), so routing a detected
    secret or prompt-injection payload through this same quarantine gate
    keeps it out of retrieval until an operator reviews it -- the same
    protection this function already gave spam, extended to content that is
    actively dangerous rather than merely low-quality. Only HIGH-confidence
    secret and injection findings quarantine on their own
    (`GuardReport.should_block`); PII and medium-confidence matches do not
    reach this function's decision at all, by that module's own design --
    see `commontrace/memory_guard.py` for why.
    """
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
    """Resolve the identity a client-address-keyed rate limiter should bucket
    this request under. See HubConfig.trusted_proxy_hops's own docstring for
    why this exists and why the default (0) never looks past
    request.client.host.

    trusted_proxy_hops > 0 reads X-Forwarded-For and takes the value
    `trusted_proxy_hops` positions from the RIGHT, not the left. A proxy
    chain is built by each hop APPENDING to the header as a request passes
    through it, so the right-most `trusted_proxy_hops` entries are the ones
    this deployment's own trusted proxies wrote; anything to their left
    (including the left-most/first entry, which is what a naive
    implementation reads) was supplied by -- and is fully controlled by --
    the original client, and trusting it lets that client mint a fresh
    identity per request just by sending a different value.

    A header with fewer entries than `trusted_proxy_hops` means a proxy that
    was supposed to append its hop did not -- log spoofing, a misconfigured
    topology, or a request that bypassed the expected proxy chain entirely.
    Falls back to request.client.host rather than trusting a value that
    cannot be attributed to a trusted hop.
    """
    if trusted_proxy_hops <= 0:
        return request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    if len(hops) >= trusted_proxy_hops:
        return hops[-trusted_proxy_hops]
    return request.client.host if request.client else "unknown"


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    """Simple per-key token bucket. `per_minute` tokens refill continuously;
    `burst` is the bucket capacity (how many calls can land back-to-back).

    `_buckets` grows one entry per distinct key ever seen and, without
    eviction, never shrinks -- a long-running Hub accumulates one bucket per
    org that has ever called contribute_trace, forever, even for an org that
    contributed once and never came back. `allow()` periodically sweeps
    buckets idle long enough to have refilled to full capacity; evicting one
    of those is a no-op change in behavior (the next call for that key
    creates a fresh bucket that starts at full capacity too), so the sweep
    trades a small amount of scan work for bounding memory to roughly the
    number of RECENTLY active keys rather than all keys ever seen.
    """

    # A bucket idle this long is guaranteed to have refilled to capacity
    # (min(capacity, ...) clamps it), so evicting it loses no state a future
    # call wouldn't reconstruct identically. Long enough that legitimate
    # bursty traffic minutes apart never sees the sweep.
    _IDLE_TTL_SECONDS = 3600.0
    # Sweep at most this often, so eviction is amortized O(1) per allow()
    # call rather than an O(n) scan of every bucket on every call.
    _SWEEP_INTERVAL_SECONDS = 300.0
    # Hard ceiling on distinct tracked keys, independent of the idle sweep.
    #
    # The sweep alone bounds memory only for keys that go idle: it evicts
    # nothing until a bucket has been untouched for _IDLE_TTL_SECONDS (1h)
    # and runs at most every 5 minutes. A client-address-keyed limiter
    # (auth attempts, /readyz) is keyed on something the peer chooses --
    # trivially, any address out of an IPv6 /64, or any X-Forwarded-For
    # value when trusted_proxy_hops is misconfigured -- so an attacker can
    # mint unbounded distinct keys and hold every one of them live inside
    # that hour-long window. That is unbounded process memory growth
    # reachable by an unauthenticated request, which is the failure this
    # limiter exists to prevent rather than to cause.
    #
    # Evicting the least-recently-used bucket when over capacity is safe in
    # the same sense the idle sweep is: a re-created bucket starts full, so
    # the worst case is that a flooding client resets its OWN limit --
    # never that it lowers anyone else's. 100k buckets is far more than any
    # real deployment's active client count and still bounded memory.
    _MAX_TRACKED_KEYS = 100_000
    # What a deny-everything limiter (per_minute <= 0) advertises as
    # Retry-After. Long, because the honest answer is "never".
    _DENY_ALL_RETRY_AFTER_SECONDS = 3600

    def __init__(self, per_minute: int, burst: int):
        self._rate_per_sec = per_minute / 60.0
        # `_capacity` floors at 1 (below) so a configured burst of 0 doesn't
        # deadlock every caller permanently -- but a fresh bucket always
        # starts full (tokens=capacity), so per_minute<=0 with that same
        # floor let every NEW key through its first `capacity` calls before
        # ever refilling: "0 requests per minute" silently meant "up to
        # `burst` free requests per distinct key, forever" instead of what
        # an operator configuring it obviously means -- deny everything.
        # per_minute<=0 is treated as an explicit "always deny" limiter
        # instead, independent of whatever burst was also configured.
        self._capacity = 0 if per_minute <= 0 else max(burst, 1)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    async def allow(self, key: str) -> bool:
        return (await self.check(key))[0]

    async def check(self, key: str) -> tuple[bool, float]:
        """(allowed, retry_after_seconds).

        `retry_after` is 0.0 when allowed, and otherwise how long until this
        bucket holds a whole token again -- which is exactly the value the
        `Retry-After` header exists to carry. Without it a refused client
        can only guess, and every client guessing (and guessing short)
        against a limiter that is already saying no is what turns one burst
        into a sustained stampede. Measured on this project's own CLI: a
        bulk `sync --push-traces` retried blind and drove 114 rejected
        requests where the honest answer was "wait ~2 seconds".
        """
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
                # A deny-everything limiter (per_minute <= 0) never refills;
                # advertising a finite wait would be a lie that invites an
                # endless retry loop.
                return False, float(self._DENY_ALL_RETRY_AFTER_SECONDS)
            return False, (1.0 - bucket.tokens) / self._rate_per_sec

    def _evict_if_over_capacity(self, now: float) -> None:
        """Make room for one new bucket. Caller already holds self._lock.

        Tries the cheap, behaviour-neutral idle sweep first; only if that
        frees nothing does it evict the least-recently-used bucket, which
        is the one whose loss costs the least (it has had the longest to
        refill).
        """
        if len(self._buckets) < self._MAX_TRACKED_KEYS:
            return
        self._sweep_idle_buckets(now)
        while len(self._buckets) >= self._MAX_TRACKED_KEYS:
            oldest = min(self._buckets, key=lambda k: self._buckets[k].last_refill)
            del self._buckets[oldest]

    def refund(self, key: str) -> None:
        """Give back one token, never exceeding capacity.

        Used by the auth-attempt limiter (hub/server.py) to charge only
        credentials that FAIL to verify. That limiter exists to bound the
        Argon2 CPU an unauthenticated source can force -- a brute-force
        defense -- but it was charging every request, successful ones
        included, so it throttled the legitimate heavy client hardest.
        Concretely: a `commontrace sync --push-traces` from one machine is
        hundreds of successful authentications from one address, and at
        60/min the ANTI-BRUTE-FORCE limiter, not the per-org fair-use one,
        became the binding constraint on this product's own documented
        onboarding.

        Refunding on success puts each limiter back on the question it can
        actually answer: this one bounds work from credentials that do not
        verify, and the per-org read limiter bounds work from credentials
        that do -- where the caller is authenticated, accountable and
        metered.
        """
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is not None:
                bucket.tokens = min(self._capacity, bucket.tokens + 1.0)

    def _sweep_idle_buckets(self, now: float) -> None:
        """Evict buckets idle long enough to have fully refilled. Caller
        already holds self._lock -- this is not re-entrant on its own."""
        stale_keys = [
            key for key, bucket in self._buckets.items()
            if now - bucket.last_refill >= self._IDLE_TTL_SECONDS
        ]
        for key in stale_keys:
            del self._buckets[key]
        self._last_sweep = now


class RateLimiterBackend(Protocol):
    """The structural contract both backends satisfy. `check`/`refund` are
    the load-bearing pair every real caller in this codebase uses --
    hub/server.py's ApiKeyAuthMiddleware and hub/crud.py's per-write-op and
    KB-submission limiters -- called synchronously, un-awaited, from inside
    `async def` functions, so these signatures must stay identical across
    backends, not merely similar. `allow` is the simpler boolean-only form,
    kept for a caller (this module's own tests included) that has no use
    for `retry_after` or the refund-on-success pattern.

    PostgresRateLimiter did not implement `check`/`refund` until this
    docstring described them as the real contract -- it only had `allow`,
    which no production call site actually calls, so
    HUB_RATE_LIMIT_BACKEND=postgres (the documented configuration for any
    multi-replica deployment) raised AttributeError on every single
    request through the auth middleware. Nothing exercised that backend
    through the middleware to catch it; hub/tests/test_auth_middleware.py
    only ever constructed it with the in-memory RateLimiter."""

    async def allow(self, key: str) -> bool: ...
    async def check(self, key: str) -> tuple[bool, float]: ...
    def refund(self, key: str) -> None: ...


def _to_asyncpg_dsn(database_url: str) -> str:
    """hub/config.py's database_url is a SQLAlchemy URL, e.g.
    postgresql+asyncpg://user:pass@host/db (hub/.env.example requires the
    asyncpg driver suffix explicitly). asyncpg.create_pool() itself wants a
    plain libpq-style DSN with no SQLAlchemy driver suffix on the scheme."""
    prefix = "postgresql+asyncpg://"
    if database_url.startswith(prefix):
        return "postgresql://" + database_url[len(prefix):]
    return database_url


# hub_rate_limit_buckets is owned entirely by this module, not hub/models.py's
# Base metadata -- created with CREATE TABLE IF NOT EXISTS (see
# _SharedPgPool._setup) rather than an Alembic migration, and never touched
# by `alembic upgrade head`.
_RATE_LIMIT_DDL = """
    CREATE TABLE IF NOT EXISTS hub_rate_limit_buckets (
        limiter_name text NOT NULL,
        bucket_key text NOT NULL,
        tokens double precision NOT NULL,
        last_refill timestamptz NOT NULL,
        PRIMARY KEY (limiter_name, bucket_key)
    )
"""

# $1=limiter_name $2=bucket_key $3=capacity $4=rate_per_sec. Upserts the
# refilled (but not yet decremented) token count -- a fresh key inserts at
# full capacity, exactly like RateLimiter._Bucket's initial state; an
# existing key's tokens refill by elapsed-time * rate, capped at capacity.
# Run inside an explicit transaction (see _allow_async) together with
# _RATE_LIMIT_DECREMENT_SQL below: this statement's INSERT/UPDATE holds a
# row lock on that (limiter_name, bucket_key) row until COMMIT, so no
# concurrent caller for the same key can interleave between this refill and
# the decrement that follows it in the same transaction.
#
# This is deliberately NOT one combined "refill and maybe decrement in a
# single statement" (an earlier version tried a data-modifying CTE feeding a
# second UPDATE against the same base table) -- Postgres statements
# (including every WITH-clause in one command) share one snapshot, so a
# row this same command just INSERTed does not yet exist as far as a
# later part of that SAME command's target-table scan is concerned, and an
# UPDATE chained that way against a freshly-inserted row silently matches
# zero rows. Two statements in one transaction, as below, do not have that
# problem: each one sees every effect of the statements before it in the
# same transaction.
_RATE_LIMIT_UPSERT_SQL = """
    INSERT INTO hub_rate_limit_buckets (limiter_name, bucket_key, tokens, last_refill)
    VALUES ($1, $2, $3, now())
    ON CONFLICT (limiter_name, bucket_key) DO UPDATE
    SET tokens = LEAST(
            $3,
            hub_rate_limit_buckets.tokens
            + EXTRACT(EPOCH FROM (now() - hub_rate_limit_buckets.last_refill)) * $4
        ),
        last_refill = now()
    RETURNING tokens
"""

# Only ever run when the caller has already decided (from the refill above,
# within the same transaction and thus the same row lock) that tokens >= 1 --
# so no WHERE tokens >= 1 guard is needed here to stay race-safe.
_RATE_LIMIT_DECREMENT_SQL = """
    UPDATE hub_rate_limit_buckets SET tokens = tokens - 1
    WHERE limiter_name = $1 AND bucket_key = $2
"""

_RATE_LIMIT_SWEEP_SQL = """
    DELETE FROM hub_rate_limit_buckets
    WHERE limiter_name = $1 AND last_refill < now() - ($2 * interval '1 second')
"""

# $1=limiter_name $2=bucket_key $3=capacity. A no-op (0 rows) if this key
# has no row yet -- refund only ever follows a successful check()/allow(),
# which always leaves a row behind, so that case is defensive rather than
# expected. Does NOT refresh last_refill: this adds a token, it is not a
# request the bucket is being asked to account for at "now".
_RATE_LIMIT_REFUND_SQL = """
    UPDATE hub_rate_limit_buckets SET tokens = LEAST($3, tokens + 1)
    WHERE limiter_name = $1 AND bucket_key = $2
"""

# How long __init__ waits for the background pool thread to finish startup
# (connect + CREATE TABLE IF NOT EXISTS) before giving up. Without a bound,
# a background-thread failure that happens *before* the try/except around
# pool setup can run (e.g. the `import asyncpg` below itself failing in a
# broken install) would kill that thread without ever signalling
# `_ready`, and __init__ would hang forever instead of raising.
_POOL_STARTUP_TIMEOUT_SECONDS = 30.0

# How long a rate-limit decision may take before the caller gives up. Kept
# from the blocking implementation this replaced: past this, the database
# is broken rather than slow, and a request needs an answer more than it
# needs to keep waiting for one.
_POOL_CALL_TIMEOUT_SECONDS = 10.0


class _SharedPgPool:
    """One background thread running its own asyncio event loop, and one
    asyncpg connection pool on it, per distinct database DSN -- shared by
    every PostgresRateLimiter instance constructed against that DSN in this
    process, via the process-wide `_shared_pools` cache below.

    hub/server.py's build_app() (out of scope for this change) calls
    make_rate_limiter/make_read_rate_limiter/make_auth_rate_limiter
    independently, each producing its own PostgresRateLimiter when
    HUB_RATE_LIMIT_BACKEND=postgres, with no way to hand them a shared
    resource explicitly. Without this cache, that would open three
    background threads and up to 3x the Postgres connections per replica
    (each `PostgresRateLimiter.__init__` calling `asyncpg.create_pool` on
    its own) for what is, from Postgres's point of view, the same workload
    against the same table -- this cache is what keeps it to one thread and
    one pool (up to `_POOL_MAX_SIZE` connections) per replica instead.
    """

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
        # Imported here, not at module level: only the postgres backend
        # needs it, and this whole method body -- import included -- runs
        # inside the try/except that reports failure back to __init__
        # rather than silently killing this thread before `ready` is ever
        # set (which would otherwise hang __init__'s bounded wait above
        # until it times out, instead of failing with the real cause).
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
                await conn.execute(_RATE_LIMIT_DDL)
        except asyncpg.exceptions.DuplicateTableError:
            # Another replica created it between our IF NOT EXISTS check and
            # the CREATE -- the table exists either way, which is all this
            # cares about.
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
    """Postgres-table-backed token bucket: same algorithm as `RateLimiter`
    above (continuous refill, capacity cap, one token per `allow()`), but
    bucket state lives in a `hub_rate_limit_buckets` row shared by every
    replica instead of a process-local dict -- so N replicas enforce one
    real limit per key instead of each granting its own N-way allowance.
    Rows are namespaced by `limiter_name` (the write/read/auth limiters
    below all share one table) plus `bucket_key` (the same key `allow()`
    always took -- an org id or a client address).

    Interface note: `allow()`/`check()` must stay synchronous and non-
    blocking-to-the-caller in the async sense (see RateLimiterBackend's
    docstring) -- real callers (hub/server.py, hub/crud.py) call them
    synchronously, un-awaited, from inside async functions, so neither
    signature can become a coroutine without changing every call site. A
    real query can't simply be awaited from a sync method, so the actual
    connection pool lives on a dedicated background thread (`_SharedPgPool`
    above); `check()`/`allow()` hand the query to that thread with
    `asyncio.run_coroutine_threadsafe(...).result(...)`, which blocks the
    CALLING thread until the transaction completes. Concretely: on the
    HUB_RATE_LIMIT_BACKEND=postgres path, every authenticated request
    (ApiKeyAuthMiddleware calls both the auth and the read limiter) and
    every contribute_trace/amend_trace/submit_kb_entry call blocks the ASGI
    event loop for that one small transaction (an upsert-refill and a
    conditional decrement, see `_refill_and_maybe_decrement`) -- typically
    sub-millisecond to a few ms against a co-located Postgres, but during
    it NO other request on that replica's event loop makes progress
    either. That is a real throughput trade-off, accepted deliberately to
    preserve the existing interface without adding a Redis dependency.
    `refund()` avoids it entirely by not waiting for its write (see its own
    docstring). See hub/DEPLOYMENT.md section 6.
    """

    _IDLE_TTL_SECONDS = RateLimiter._IDLE_TTL_SECONDS
    _SWEEP_INTERVAL_SECONDS = RateLimiter._SWEEP_INTERVAL_SECONDS

    def __init__(self, per_minute: int, burst: int, database_url: str, limiter_name: str):
        # Same per_minute<=0 -> deny-everything floor as RateLimiter, and
        # for the same reason (see RateLimiter.__init__): a fresh bucket
        # for a never-seen key must not start pre-filled when the operator
        # configured zero throughput.
        self._rate_per_sec = per_minute / 60.0
        self._capacity = 0.0 if per_minute <= 0 else float(max(burst, 1))
        self._limiter_name = limiter_name
        self._last_sweep = time.monotonic()
        self._shared = _get_shared_pool(_to_asyncpg_dsn(database_url))

    async def _await_on_pool_loop(self, coro):
        """Run `coro` on the pool's background loop and AWAIT the result.

        The pool lives on its own loop in its own thread (see
        `_SharedPgPool`), so the work has to be handed across; the only
        question is what the caller does while it runs.
        `run_coroutine_threadsafe` returns a concurrent.futures.Future, and
        calling `.result()` on it blocks the CALLING thread -- which, for
        every real caller here, is the ASGI event loop, so no other request
        on the replica progressed until the query came back.
        `asyncio.wrap_future` adapts that same future into an awaitable this
        loop can suspend on instead, which frees it to serve other requests
        for the duration. Same pool, same thread, same query, same
        cross-replica correctness -- the difference is entirely in who
        waits.

        This does NOT buy throughput, and the measurement says so:
        hub/bench_concurrency.py reports 165 -> 177 rps at 128 concurrent
        clients, which is noise. Nor is the connection pool the ceiling --
        raising it from 5 to 32 moved nothing. On a benchmark where EVERY
        request needs a limiter decision, freeing the loop cannot help,
        because everything it could switch to is queued behind the same
        work. What this fixes is the case that benchmark cannot show: work
        that does NOT need a limiter decision -- a health check, another
        endpoint, an in-flight response -- no longer waits behind one.
        Measured directly: an unauthenticated /healthz polled while 32
        clients hammer the limited path goes from p50 42.3ms / p99 62.8ms
        to p50 2.3ms / p99 8.6ms, completing 448 probes in the window
        instead of 81. That is the real defect this closes -- a replica
        whose health endpoint answers in 63ms because something ELSE is
        rate-limited looks unhealthy to a load balancer for reasons that
        have nothing to do with its health.
        See hub/SCALING.md "Concurrency: measured".

        The 10s ceiling is kept from the blocking version it replaces: a
        rate-limit decision that has not come back in ten seconds is a
        broken database, not a slow one, and the caller needs an answer
        rather than an indefinite wait.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._shared.loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=_POOL_CALL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            # wrap_future's own cancellation does not reach across loops on
            # its own; cancel the real future so the query is not left
            # running on the pool thread after nobody is waiting for it.
            future.cancel()
            raise

    async def allow(self, key: str) -> bool:
        return await self._await_on_pool_loop(self._allow_async(key))

    async def _allow_async(self, key: str) -> bool:
        allowed, _retry_after = await self._refill_and_maybe_decrement(key)
        return allowed

    async def check(self, key: str) -> tuple[bool, float]:
        """(allowed, retry_after_seconds) -- the method every real caller in
        this codebase actually uses (hub/server.py's ApiKeyAuthMiddleware,
        hub/crud.py's per-write-op and KB-submission limiters). `allow()`
        above is kept for direct testing and any external caller wanting
        the plain boolean, but until this method existed on this class,
        `HUB_RATE_LIMIT_BACKEND=postgres` -- the officially documented
        configuration for any multi-replica deployment -- made every
        single request through the auth middleware raise AttributeError:
        RateLimiter (the in-memory default) grew `.check()`/`.refund()` at
        some point, and every real call site was updated to use them, but
        this class was never given matching methods, and nothing exercised
        the Postgres backend through that middleware to catch it. See
        RateLimiter.check's docstring for what `retry_after` is for."""
        return await self._await_on_pool_loop(self._check_async(key))

    async def _check_async(self, key: str) -> tuple[bool, float]:
        allowed, tokens = await self._refill_and_maybe_decrement(key)
        if allowed:
            return True, 0.0
        if self._rate_per_sec <= 0:
            # Same rationale as RateLimiter.check: a deny-everything
            # limiter never refills, so advertising a finite wait would be
            # a lie that invites an endless retry loop.
            return False, float(RateLimiter._DENY_ALL_RETRY_AFTER_SECONDS)
        return False, (1.0 - tokens) / self._rate_per_sec

    async def _refill_and_maybe_decrement(self, key: str) -> tuple[bool, float]:
        """Shared by `_allow_async` and `_check_async`: refill this bucket
        (capped at capacity) and decrement it by one token IF it now holds
        at least one, all inside a single transaction. Returns
        (allowed, tokens_after) -- `tokens_after` is the refilled-but-not-
        decremented count, which `_check_async` needs to compute
        `retry_after` and `_allow_async` discards.

        Two statements in one transaction rather than one combined SQL
        statement -- see _RATE_LIMIT_UPSERT_SQL's comment for why a
        single-statement version of this is not just an optimization but
        actually broken for a never-before-seen key. The row lock the
        UPSERT takes is held until COMMIT, so nothing else can touch this
        (limiter_name, key) row between the refill and the decrement.
        """
        async with self._shared.pool.acquire() as conn, conn.transaction():
            tokens = await conn.fetchval(
                _RATE_LIMIT_UPSERT_SQL, self._limiter_name, key, self._capacity, self._rate_per_sec
            )
            allowed = tokens >= 1.0
            if allowed:
                await conn.execute(_RATE_LIMIT_DECREMENT_SQL, self._limiter_name, key)
        self._maybe_sweep()
        return allowed, tokens

    def refund(self, key: str) -> None:
        """Give back one token, never exceeding capacity -- see
        RateLimiter.refund's docstring for why this exists (charging only
        credentials that fail to verify, not every attempt).

        Fire-and-forget, deliberately unlike `allow`/`check`: the caller
        (hub/server.py, immediately after already deciding the request is
        authenticated) does not need this write to land before it can
        proceed, and there is no correctness cost to occasionally losing
        one to a transient failure -- that bucket simply refills a moment
        later via time instead of via the refund. Not waiting also means
        this call never blocks the ASGI event loop at all, unlike
        `allow`/`check`'s documented trade-off above.
        """
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
        """Mirrors RateLimiter._sweep_idle_buckets: bound table growth by
        periodically deleting rows idle long enough to have fully refilled
        (evicting one loses no state a future call wouldn't reconstruct
        identically). Runs only on the shared pool's own background-loop
        thread (called from `_allow_async`, never concurrently with
        itself for a given `PostgresRateLimiter`), so -- unlike
        RateLimiter's version -- it needs no lock. Fire-and-forget: cleanup
        must not add latency to the `allow()` call that happened to
        trigger it."""
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
        """Deliberately a no-op: the connection pool and background thread
        this instance uses are shared (keyed by DSN, see `_SharedPgPool`)
        with every other PostgresRateLimiter in this process, potentially
        including the write/read/auth limiters hub/server.py's build_app()
        constructs independently -- tearing either down here would break
        those. They are process-lifetime by design (the thread is a
        daemon, so it exits with the process; nothing else ever needs to
        close them). Kept as a method, rather than removed, so callers that
        unconditionally call `.close()` for cleanup (this module's own
        tests included) don't need to special-case this backend."""

    def __repr__(self) -> str:
        return f"PostgresRateLimiter(limiter_name={self._limiter_name!r}, capacity={self._capacity!r})"


def _make_limiter(config: HubConfig, per_minute: int, burst: int, limiter_name: str) -> RateLimiterBackend:
    if config.rate_limit_backend == "postgres":
        return PostgresRateLimiter(
            per_minute=per_minute, burst=burst, database_url=config.database_url, limiter_name=limiter_name
        )
    return RateLimiter(per_minute=per_minute, burst=burst)


def make_rate_limiter(config: HubConfig) -> RateLimiterBackend:
    return _make_limiter(config, config.rate_limit_per_minute, config.rate_limit_burst, "write")


def make_read_rate_limiter(config: HubConfig) -> RateLimiterBackend:
    """Keyed by org_id, applied to EVERY authenticated request in
    ApiKeyAuthMiddleware -- unlike `make_rate_limiter`'s bucket, which only
    ever gates the two write tools."""
    return _make_limiter(config, config.read_rate_limit_per_minute, config.read_rate_limit_burst, "read")


def make_auth_rate_limiter(config: HubConfig) -> RateLimiterBackend:
    """Keyed by client address, checked before verification even runs --
    bounds CPU spent verifying credentials from one source rather than only
    counting failures after paying for them. Most requests never reach the
    expensive path this defends (hub/auth.py's indexed key_hmac lookup
    handles them for ~1ms), but the legacy Argon2 fallback for unmigrated
    keys is still exactly as expensive as before, so this stays in place."""
    return _make_limiter(config, config.auth_attempts_per_minute, config.auth_attempts_burst, "auth")
