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
  4. a cheap suspicion heuristic (this module)           -> soft: store
     quarantined=True, excluded from search_traces, pending manual review

Rate limiting is an in-memory token bucket keyed by org_id. That is a known,
documented MVP limitation: it resets on process restart and does not
coordinate across multiple server instances. A horizontally-scaled
deployment needs a shared store (Redis INCR+EXPIRE, or a Postgres-backed
bucket) -- noted as a follow-up in hub/README.md, not implemented here to
avoid adding a Redis dependency for a single-process MVP.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass

from hub.config import HubConfig

_URL_RE = re.compile(r"https?://", re.IGNORECASE)


class TraceRejected(ValueError):
    """Hard rejection: the trace was not stored at all."""


class RateLimited(Exception):
    """Hard rejection: the org is over its contribute_trace rate limit."""


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
    # Not every caller's `fields` includes this -- amend_trace's wire dict
    # never did and never sets a new agent_type -- so it is read the same
    # defensive way as `tags` above rather than assumed present.
    agent_type = fields.get("agent_type") or ""

    for value, name in (
        (title, "title"),
        (context_text, "context_text"),
        (solution_text, "solution_text"),
        (agent_type, "agent_type"),
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

    serialized_size = len(json.dumps(fields, ensure_ascii=False).encode("utf-8"))
    if serialized_size > config.max_trace_bytes:
        raise TraceRejected(f"trace exceeds {config.max_trace_bytes} bytes serialized ({serialized_size})")


def suspicion_reason(fields: dict, config: HubConfig) -> str | None:
    """Return a short human-readable reason to quarantine this trace, or
    None if it looks fine. Deliberately simple and named as a placeholder:
    this is not a moderation system, just a first line of defense against
    obvious spam. Replace/extend as real abuse patterns are observed."""
    text = " ".join(str(fields.get(k, "")) for k in ("title", "context_text", "solution_text"))

    url_count = len(_URL_RE.findall(text))
    if url_count > config.suspect_url_threshold:
        return f"contains {url_count} URLs (> {config.suspect_url_threshold} threshold)"

    stripped = text.strip()
    if stripped and len(set(stripped.lower())) <= 3 and len(stripped) > 20:
        return "content has near-zero character diversity (likely filler/spam)"

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

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self._capacity), last_refill=now)
                self._buckets[key] = bucket
            elapsed = now - bucket.last_refill
            bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._rate_per_sec)
            bucket.last_refill = now
            if now - self._last_sweep >= self._SWEEP_INTERVAL_SECONDS:
                self._sweep_idle_buckets(now)
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False

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


def make_rate_limiter(config: HubConfig) -> RateLimiter:
    return RateLimiter(per_minute=config.rate_limit_per_minute, burst=config.rate_limit_burst)


def make_read_rate_limiter(config: HubConfig) -> RateLimiter:
    """Keyed by org_id, applied to EVERY authenticated request in
    ApiKeyAuthMiddleware -- unlike `make_rate_limiter`'s bucket, which only
    ever gates the two write tools."""
    return RateLimiter(per_minute=config.read_rate_limit_per_minute, burst=config.read_rate_limit_burst)


def make_auth_rate_limiter(config: HubConfig) -> RateLimiter:
    """Keyed by client address, checked before Argon2 verification even
    runs -- bounds CPU spent verifying credentials from one source rather
    than only counting failures after paying for them."""
    return RateLimiter(per_minute=config.auth_attempts_per_minute, burst=config.auth_attempts_burst)
