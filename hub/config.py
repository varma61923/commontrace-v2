"""Environment-driven configuration for the Hub server."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass

from hub.secrets_provider import env_secret

DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 200
MAX_SEARCH_OFFSET = 100_000


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_int_in_range(name: str, default: int, lo: int, hi: int) -> int:
    value = _env_int(name, default)
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_cidr_list(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return ()
    entries = tuple(item.strip() for item in raw.split(",") if item.strip())
    for entry in entries:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError as exc:
            raise ValueError(f"{name} contains an invalid CIDR entry {entry!r}: {exc}") from None
    return entries


@dataclass(frozen=True)
class HubConfig:
    database_url: str

    host: str = "127.0.0.1"
    port: int = 8420
    streamable_http_path: str = "/mcp"
    max_request_body_bytes: int = 1_048_576

    max_title_chars: int = 500
    max_text_chars: int = 20_000
    max_tags: int = 20
    max_tag_chars: int = 64
    max_trace_bytes: int = 65_536
    rate_limit_per_minute: int = 120
    rate_limit_burst: int = 30
    suspect_url_threshold: int = 5

    rate_limit_backend: str = "memory"

    read_rate_limit_per_minute: int = 300
    read_rate_limit_burst: int = 60

    auth_attempts_per_minute: int = 60
    auth_attempts_burst: int = 20

    readyz_rate_limit_per_minute: int = 120
    readyz_rate_limit_burst: int = 30

    trusted_proxy_hops: int = 0

    ip_allowlist: tuple[str, ...] = ()

    admin_token: str = ""
    metrics_token: str = ""

    operator_org_id: str = ""

    console_secret: str = ""

    data_region: str = ""
    operator_legal_name: str = ""
    operator_support_contact: str = ""

    signup_enabled: bool = False

    rest_api_enabled: bool = False

    otlp_ingest_enabled: bool = False

    connectors_enabled: bool = False
    connector_sweep_interval_seconds: int = 300
    commons_export_enabled: bool = False

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_team: str = ""
    stripe_price_scale: str = ""

    ledger_signing_key: str = ""

    auth_cache_seconds: float = 0.0
    encryption_key: str = ""
    encryption_key_previous: str = ""

    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks: str = ""
    oidc_jwks_uri: str = ""

    allow_rls_bypass: bool = False

    require_rls: bool = False

    allow_insecure_http: bool = False

    commons_enabled: bool = True

    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_timeout: int = 30
    db_statement_timeout_ms: int = 30_000
    request_timeout_seconds: int = 60
    max_concurrent_requests: int = 512
    db_pool_recycle: int = 1800

    graceful_shutdown_seconds: int = 30

    alert_scheduler_enabled: bool = False
    alert_scheduler_interval_seconds: int = 300

    webhook_scheduler_enabled: bool = False
    webhook_scheduler_interval_seconds: int = 30
    webhook_scheduler_batch_size: int = 100

    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.rate_limit_backend not in ("memory", "postgres"):
            raise ValueError(
                f"HUB_RATE_LIMIT_BACKEND must be 'memory' or 'postgres', got {self.rate_limit_backend!r}"
            )
        self.cipher()

    def cipher(self):
        from hub.encryption import EnvelopeCipher

        return EnvelopeCipher.from_config(self.encryption_key, self.encryption_key_previous)

    def identity_provider(self):
        import json

        from hub.sso import IdentityProvider

        if not self.oidc_issuer or not self.oidc_audience:
            return None
        jwks = None
        if self.oidc_jwks:
            try:
                jwks = json.loads(self.oidc_jwks)
            except ValueError as exc:
                raise RuntimeError(
                    f"HUB_OIDC_JWKS is not valid JSON: {exc}"
                ) from None
        if jwks is None and not self.oidc_jwks_uri:
            raise RuntimeError(
                "HUB_OIDC_ISSUER and HUB_OIDC_AUDIENCE are set, so SSO is "
                "configured, but neither HUB_OIDC_JWKS nor HUB_OIDC_JWKS_URI "
                "names where to find the signing keys -- refusing to start "
                "in a state where no token could ever verify."
            )
        return IdentityProvider(
            issuer=self.oidc_issuer, audience=self.oidc_audience,
            jwks=jwks, jwks_uri=self.oidc_jwks_uri,
        )

    def validate_transport_safety(self) -> None:
        if self.allow_insecure_http:
            return
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError(
                f"refusing to start: HUB_HOST={self.host!r} is not loopback-only, and this Hub "
                "speaks plain HTTP -- every call carries the org's API key as a plaintext Bearer "
                "token. Put a TLS-terminating reverse proxy in front and bind HUB_HOST to "
                "127.0.0.1 (or localhost/::1), or set HUB_ALLOW_INSECURE_HTTP=true to "
                "acknowledge this is intentional (e.g. TLS is terminated elsewhere on a "
                "network you trust)."
            )

    @classmethod
    def from_env(cls) -> HubConfig:
        database_url = env_secret("HUB_DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "HUB_DATABASE_URL is required (see hub/.env.example). "
                "Refusing to start with no configured database rather than "
                "guessing a default connection string."
            )
        from hub import kms

        encryption_key, encryption_key_previous = kms.resolve(os.environ, env_secret)
        return cls(
            database_url=database_url,
            host=os.environ.get("HUB_HOST", "127.0.0.1"),
            port=_env_int_in_range("HUB_PORT", 8420, 1, 65535),
            streamable_http_path=os.environ.get("HUB_STREAMABLE_HTTP_PATH", "/mcp"),
            max_request_body_bytes=_env_int_in_range("HUB_MAX_REQUEST_BODY_BYTES", 1_048_576, 1024, 64 * 1024 * 1024),
            max_title_chars=_env_int_in_range("HUB_MAX_TITLE_CHARS", 500, 1, 100_000),
            max_text_chars=_env_int_in_range("HUB_MAX_TEXT_CHARS", 20_000, 1, 10_000_000),
            max_tags=_env_int_in_range("HUB_MAX_TAGS", 20, 1, 1000),
            max_tag_chars=_env_int_in_range("HUB_MAX_TAG_CHARS", 64, 1, 10_000),
            max_trace_bytes=_env_int_in_range("HUB_MAX_TRACE_BYTES", 65_536, 1024, 64 * 1024 * 1024),
            rate_limit_per_minute=_env_int_in_range("HUB_RATE_LIMIT_PER_MINUTE", 120, 0, 10_000_000),
            rate_limit_burst=_env_int_in_range("HUB_RATE_LIMIT_BURST", 30, 0, 1_000_000),
            suspect_url_threshold=_env_int_in_range("HUB_SUSPECT_URL_THRESHOLD", 5, 0, 10_000),
            rate_limit_backend=os.environ.get("HUB_RATE_LIMIT_BACKEND", "memory"),
            read_rate_limit_per_minute=_env_int_in_range("HUB_READ_RATE_LIMIT_PER_MINUTE", 300, 0, 10_000_000),
            read_rate_limit_burst=_env_int_in_range("HUB_READ_RATE_LIMIT_BURST", 60, 0, 1_000_000),
            auth_attempts_per_minute=_env_int_in_range("HUB_AUTH_ATTEMPTS_PER_MINUTE", 60, 0, 10_000_000),
            auth_attempts_burst=_env_int_in_range("HUB_AUTH_ATTEMPTS_BURST", 20, 0, 1_000_000),
            readyz_rate_limit_per_minute=_env_int_in_range("HUB_READYZ_RATE_LIMIT_PER_MINUTE", 120, 0, 10_000_000),
            readyz_rate_limit_burst=_env_int_in_range("HUB_READYZ_RATE_LIMIT_BURST", 30, 0, 1_000_000),
            trusted_proxy_hops=_env_int_in_range("HUB_TRUSTED_PROXY_HOPS", 0, 0, 16),
            ip_allowlist=_env_cidr_list("HUB_IP_ALLOWLIST"),
            allow_insecure_http=_env_bool("HUB_ALLOW_INSECURE_HTTP", False),
            allow_rls_bypass=_env_bool("HUB_ALLOW_RLS_BYPASS", False),
            require_rls=_env_bool("HUB_REQUIRE_RLS", False),
            auth_cache_seconds=_env_int_in_range("HUB_AUTH_CACHE_SECONDS", 0, 0, 60),
            encryption_key=encryption_key,
            encryption_key_previous=encryption_key_previous,
            admin_token=env_secret("HUB_ADMIN_TOKEN"),
            metrics_token=env_secret("HUB_METRICS_TOKEN"),
            operator_org_id=os.environ.get("HUB_OPERATOR_ORG_ID", ""),
            console_secret=env_secret("HUB_CONSOLE_SECRET"),
            data_region=os.environ.get("HUB_DATA_REGION", ""),
            operator_legal_name=os.environ.get("HUB_OPERATOR_LEGAL_NAME", ""),
            operator_support_contact=os.environ.get("HUB_OPERATOR_SUPPORT_CONTACT", ""),
            signup_enabled=_env_bool("HUB_SIGNUP_ENABLED", False),
            rest_api_enabled=_env_bool("HUB_REST_API_ENABLED", False),
            otlp_ingest_enabled=_env_bool("HUB_OTLP_INGEST_ENABLED", False),
            connectors_enabled=_env_bool("HUB_CONNECTORS_ENABLED", False),
            connector_sweep_interval_seconds=_env_int_in_range(
                "HUB_CONNECTOR_SWEEP_INTERVAL_SECONDS", 300, 10, 86_400),
            commons_export_enabled=_env_bool("HUB_COMMONS_EXPORT_ENABLED", False),
            stripe_secret_key=env_secret("HUB_STRIPE_SECRET_KEY"),
            stripe_webhook_secret=env_secret("HUB_STRIPE_WEBHOOK_SECRET"),
            stripe_price_team=os.environ.get("HUB_STRIPE_PRICE_TEAM", ""),
            stripe_price_scale=os.environ.get("HUB_STRIPE_PRICE_SCALE", ""),
            ledger_signing_key=env_secret("HUB_LEDGER_SIGNING_KEY"),
            oidc_issuer=os.environ.get("HUB_OIDC_ISSUER", ""),
            oidc_audience=os.environ.get("HUB_OIDC_AUDIENCE", ""),
            oidc_jwks=os.environ.get("HUB_OIDC_JWKS", ""),
            oidc_jwks_uri=os.environ.get("HUB_OIDC_JWKS_URI", ""),
            commons_enabled=_env_bool("HUB_COMMONS_ENABLED", True),
            db_pool_size=_env_int_in_range("HUB_DB_POOL_SIZE", 10, 1, 1000),
            db_max_overflow=_env_int_in_range("HUB_DB_MAX_OVERFLOW", 5, 0, 1000),
            db_pool_timeout=_env_int_in_range("HUB_DB_POOL_TIMEOUT", 30, 1, 3600),
            db_statement_timeout_ms=_env_int_in_range(
                "HUB_DB_STATEMENT_TIMEOUT_MS", 30_000, 0, 3_600_000),
            request_timeout_seconds=_env_int_in_range(
                "HUB_REQUEST_TIMEOUT_SECONDS", 60, 0, 3600),
            max_concurrent_requests=_env_int_in_range(
                "HUB_MAX_CONCURRENT_REQUESTS", 512, 0, 100_000),
            db_pool_recycle=_env_int_in_range("HUB_DB_POOL_RECYCLE", 1800, -1, 86_400),
            graceful_shutdown_seconds=_env_int_in_range("HUB_GRACEFUL_SHUTDOWN_SECONDS", 30, 0, 3600),
            alert_scheduler_enabled=_env_bool("HUB_ALERT_SCHEDULER_ENABLED", False),
            alert_scheduler_interval_seconds=_env_int_in_range(
                "HUB_ALERT_SCHEDULER_INTERVAL_SECONDS", 300, 10, 86_400),
            webhook_scheduler_enabled=_env_bool("HUB_WEBHOOK_SCHEDULER_ENABLED", False),
            webhook_scheduler_interval_seconds=_env_int_in_range(
                "HUB_WEBHOOK_SCHEDULER_INTERVAL_SECONDS", 30, 5, 86_400),
            webhook_scheduler_batch_size=_env_int_in_range(
                "HUB_WEBHOOK_SCHEDULER_BATCH_SIZE", 100, 1, 10_000),
            log_level=os.environ.get("HUB_LOG_LEVEL", "INFO"),
        )
