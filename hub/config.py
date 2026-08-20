"""Environment-driven configuration for the Hub server.

Every setting has a HUB_-prefixed env var (documented in hub/.env.example).
No secrets have defaults that are usable in production; DATABASE_URL has no
default at all so a misconfigured deploy fails loudly instead of silently
falling back to something unsafe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# search_traces pagination. MAX is a hard ceiling applied to whatever a
# client asks for: an unbounded limit lets one caller pull an org's entire
# trace store in a single request, which is both a performance and a
# blast-radius concern.
DEFAULT_SEARCH_LIMIT = 50  # matches the previous hard-coded cap
MAX_SEARCH_LIMIT = 200


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class HubConfig:
    # --- Persistence ---
    database_url: str

    # --- Transport ---
    host: str = "127.0.0.1"
    port: int = 8420
    streamable_http_path: str = "/mcp"

    # --- Abuse controls (contribute_trace) ---
    max_title_chars: int = 500
    max_text_chars: int = 20_000  # context_text / solution_text each
    max_tags: int = 20
    max_tag_chars: int = 64
    max_trace_bytes: int = 65_536  # serialized JSON size ceiling for one trace
    rate_limit_per_minute: int = 20  # contribute_trace calls, per org
    rate_limit_burst: int = 5
    suspect_url_threshold: int = 5  # >N URLs in one submission -> quarantine

    # --- Auth ---
    api_key_header: str = "Authorization"  # expects "Bearer <key>"

    # --- Connection pool ---
    # pool_size * number_of_replicas must stay below Postgres max_connections.
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_timeout: int = 30       # seconds to wait for a free connection
    db_pool_recycle: int = 1800     # recycle connections older than 30 min

    # --- Lifecycle ---
    graceful_shutdown_seconds: int = 30

    # --- Misc ---
    log_level: str = "INFO"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> HubConfig:
        database_url = os.environ.get("HUB_DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "HUB_DATABASE_URL is required (see hub/.env.example). "
                "Refusing to start with no configured database rather than "
                "guessing a default connection string."
            )
        return cls(
            database_url=database_url,
            host=os.environ.get("HUB_HOST", "127.0.0.1"),
            port=_env_int("HUB_PORT", 8420),
            streamable_http_path=os.environ.get("HUB_STREAMABLE_HTTP_PATH", "/mcp"),
            max_title_chars=_env_int("HUB_MAX_TITLE_CHARS", 500),
            max_text_chars=_env_int("HUB_MAX_TEXT_CHARS", 20_000),
            max_tags=_env_int("HUB_MAX_TAGS", 20),
            max_tag_chars=_env_int("HUB_MAX_TAG_CHARS", 64),
            max_trace_bytes=_env_int("HUB_MAX_TRACE_BYTES", 65_536),
            rate_limit_per_minute=_env_int("HUB_RATE_LIMIT_PER_MINUTE", 20),
            rate_limit_burst=_env_int("HUB_RATE_LIMIT_BURST", 5),
            suspect_url_threshold=_env_int("HUB_SUSPECT_URL_THRESHOLD", 5),
            db_pool_size=_env_int("HUB_DB_POOL_SIZE", 10),
            db_max_overflow=_env_int("HUB_DB_MAX_OVERFLOW", 5),
            db_pool_timeout=_env_int("HUB_DB_POOL_TIMEOUT", 30),
            db_pool_recycle=_env_int("HUB_DB_POOL_RECYCLE", 1800),
            graceful_shutdown_seconds=_env_int("HUB_GRACEFUL_SHUTDOWN_SECONDS", 30),
            log_level=os.environ.get("HUB_LOG_LEVEL", "INFO"),
        )
