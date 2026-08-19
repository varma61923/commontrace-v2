"""Abuse controls for contribute_trace.

This is a shared cross-org store: one careless or malicious contributor can
degrade retrieval quality for every other org, so contribute_trace runs
through, in order:

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


def validate_size(fields: dict, config: HubConfig) -> None:
    title = fields.get("title", "")
    context_text = fields.get("context_text", "")
    solution_text = fields.get("solution_text", "")
    tags = fields.get("tags") or []

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


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    """Simple per-key token bucket. `per_minute` tokens refill continuously;
    `burst` is the bucket capacity (how many calls can land back-to-back)."""

    def __init__(self, per_minute: int, burst: int):
        self._rate_per_sec = per_minute / 60.0
        self._capacity = max(burst, 1)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

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
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False


def make_rate_limiter(config: HubConfig) -> RateLimiter:
    return RateLimiter(per_minute=config.rate_limit_per_minute, burst=config.rate_limit_burst)
