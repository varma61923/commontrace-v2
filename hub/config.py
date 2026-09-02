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
# OFFSET/LIMIT pagination costs Postgres work proportional to `offset`
# itself (it still has to walk and discard every skipped row), unlike
# `limit` which is already bounded above. A caller-supplied offset was
# otherwise unbounded, so a very large one turns one request into a scan
# of an org's entire trace table just to throw away the results. This is
# a blunt cap, not the real fix (keyset/cursor pagination, tracked as
# follow-up work) -- but it bounds the damage a single request can do in
# the meantime without changing the offset/limit/has_more response shape
# every existing caller (including commontrace/hub_client.py) depends on.
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
    """`_env_int`, plus the range the value actually has to be in.

    Without this, every numeric setting accepted anything an int could
    parse and failed later, somewhere else, in a way that named neither the
    variable nor the reason. Concretely, all of these started a process
    that then misbehaved rather than refusing to start:

      HUB_PORT=99999            -> OSError deep in uvicorn's bind
      HUB_DB_POOL_SIZE=-1       -> a SQLAlchemy pool error on first query
      HUB_MAX_TITLE_CHARS=-5    -> every contribute_trace rejected, no
                                   message anywhere saying why
      HUB_MAX_REQUEST_BODY_BYTES=0 -> every request rejected as too large

    A deployment misconfiguration should fail at startup, naming the
    variable and the bound -- the same policy HUB_DATABASE_URL already gets
    for being absent.
    """
    value = _env_int(name, default)
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


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
    # The MCP SDK's own transport already rejects an oversized request body
    # (checking bytes actually received, not just a client-supplied
    # Content-Length, so chunked transfer-encoding can't bypass it) -- but
    # that protection comes from an *upstream default*
    # (mcp.server.streamable_http_manager.DEFAULT_MAX_REQUEST_BODY_SIZE),
    # not a value this module chose or tested. Set it explicitly so it's
    # this project's own decision, and tighter than the SDK's generic 4MiB:
    # a trace can never legitimately exceed max_trace_bytes (64KiB)
    # serialized, so 1MiB leaves generous room for JSON-RPC/MCP envelope
    # overhead while still rejecting anything trying to smuggle a much
    # larger payload through.
    max_request_body_bytes: int = 1_048_576

    # --- Abuse controls (contribute_trace) ---
    max_title_chars: int = 500
    max_text_chars: int = 20_000  # context_text / solution_text each
    max_tags: int = 20
    max_tag_chars: int = 64
    max_trace_bytes: int = 65_536  # serialized JSON size ceiling for one trace
    # Per-org write limit (contribute_trace/amend_trace/submit_kb_entry).
    #
    # Raised from 20/min burst 5. That default could not serve this
    # product's own documented onboarding: `commontrace import` a fleet's
    # existing history, then `commontrace sync --push-traces`. At 20/min a
    # 46-trace store took over two minutes and a 1,000-trace import -- the
    # size the import path exists for -- took the better part of an hour,
    # while the client (before the pacing fix in
    # commontrace/hub_client.py) simply failed most of the files instead.
    #
    # What this limiter is actually for, per this module's own rationale,
    # is protecting the shared Postgres every org writes into and keeping
    # an org's search useful to itself. Two writes per second per org
    # serves both: it is still far below what one Postgres handles, still
    # bounds a runaway agent to a rate an operator will notice long before
    # it matters, and it lets the documented first-run finish in a minute
    # rather than an hour. The burst is what a bulk push actually consumes,
    # so it moves with it.
    rate_limit_per_minute: int = 120  # contribute_trace/amend_trace calls, per org
    rate_limit_burst: int = 30
    suspect_url_threshold: int = 5  # >N URLs in one submission -> quarantine

    # --- Rate limiting: every authenticated request, and auth itself ---
    #
    # rate_limit_per_minute above only ever gated the two write tools
    # (contribute_trace/amend_trace). search_traces, get_trace, vote_trace,
    # list_tags, and commons_overlap had no limit at all: a single valid API
    # key could drive unmetered full-text search, ranking computation, and
    # MinHash corpus scans. This is a second, more generous ceiling applied
    # to EVERY authenticated request in ApiKeyAuthMiddleware, on top of (not
    # instead of) the tighter per-write-op limiter.
    read_rate_limit_per_minute: int = 300  # all authenticated requests, per org
    read_rate_limit_burst: int = 60

    # Argon2id verification is deliberately expensive CPU work
    # (hub/auth.py), run per candidate key sharing a presented key's prefix,
    # on every request carrying an Authorization header -- valid or not.
    # Without a limit, a remote attacker can flood the endpoint with
    # credentials sharing a known/guessed prefix to exhaust the process's
    # to_thread worker pool. Checked BEFORE verify_api_key runs, keyed by
    # the request's client address, so it bounds the CPU cost per source
    # rather than only counting failures after paying for them.
    auth_attempts_per_minute: int = 60
    auth_attempts_burst: int = 20

    # /readyz executes a real `SELECT 1` against the database pool and is
    # necessarily unauthenticated (an orchestrator's prober carries no
    # tenant API key), so it sits outside every limiter above. Keyed by
    # client address; default is generous relative to a real orchestrator's
    # poll interval (typically every few seconds).
    readyz_rate_limit_per_minute: int = 120
    readyz_rate_limit_burst: int = 30

    # Every client-address-keyed rate limiter above (auth_attempts,
    # read_rate_limit, readyz) is keyed off request.client.host by default --
    # the peer of the actual TCP connection reaching this process. Behind
    # ANY reverse proxy or load balancer, that is the proxy's own address for
    # every request, which silently collapses every one of those limiters
    # into one shared bucket across every real client (a self-inflicted DoS:
    # one noisy client can exhaust it for everyone) -- including the Hub's
    # own documented "loopback + sidecar TLS-terminating proxy on the same
    # host" deployment shape (validate_transport_safety's docstring above).
    #
    # 0 (default) trusts nothing but request.client.host, identical to
    # today's behavior -- safe for a deployment with no proxy in front, and
    # the only safe default: X-Forwarded-For is an ordinary client-settable
    # HTTP header, and blindly trusting it (e.g. always taking its first,
    # left-most entry, as a client-authored chain could) lets any client
    # mint a fresh rate-limit bucket per request just by sending a different
    # spoofed IP -- turning a DoS defense into a bypass, which is worse than
    # the collapsed-bucket problem it would be fixing.
    #
    # Set to the exact number of trusted reverse proxies in front of this
    # Hub (1 for a single TLS-terminating proxy or sidecar) to resolve the
    # client address as the value `trusted_proxy_hops` positions from the
    # RIGHT of X-Forwarded-For instead -- the one position in the chain a
    # trusted proxy, not an upstream client, is responsible for appending.
    # An operator must opt in explicitly because this module cannot verify
    # its own network topology; setting it when no such proxy exists lets a
    # client forge its own rate-limit identity via a spoofed header.
    trusted_proxy_hops: int = 0

    # --- Auth ---
    api_key_header: str = "Authorization"  # expects "Bearer <key>"

    # --- Operator console (hub/admin.py) ---
    # Empty (the default) means the /admin routes are NEVER REGISTERED --
    # an unauthenticated prober gets a 404 from the router, not a 401 from a
    # handler, which is the same "absent, not merely refused" property
    # commons_enabled gives the Knowledge Base tools. A deployment that has
    # not opted in has no console to attack.
    #
    # When set, this is the password half of an HTTP Basic credential (the
    # username is ignored) compared in constant time. It is a SHARED
    # OPERATOR SECRET, not a per-user login: treat it like the database URL,
    # keep it in a secret store, and rotate it by changing this value and
    # restarting. The console is read-only by design (see hub/admin.py), so
    # the worst a leaked token buys is visibility -- which is bad enough that
    # it belongs behind the same TLS and network controls as everything else.
    admin_token: str = ""

    # The org that Knowledge Base entries are published UNDER when an
    # operator accepts a community submission. Never the submitting org --
    # the same ownership rule commons_seed follows (hub/crud.py:
    # review_kb_submission), so an accepted proposal becomes operator-owned
    # substrate knowledge rather than one customer's content served to
    # another.
    #
    # Unset means the console can still REVIEW and REJECT, but its accept
    # action fails closed and shows the CLI command instead: publishing
    # under the wrong org would put a customer's id on Knowledge Base
    # content, which is the one mistake this whole boundary exists to
    # prevent, and it is not a mistake a dropdown should be able to make.
    operator_org_id: str = ""

    # --- Transport safety ---
    # The Hub itself always speaks plain HTTP (I-06: TLS termination is
    # delegated to an upstream reverse proxy) -- that is a supported,
    # documented deployment shape when `host` is loopback-only, or when a
    # proxy sits in front on the same trusted network. It stops being safe
    # the moment `host` binds a non-loopback interface directly reachable by
    # clients with no proxy in between: every call carries the org's API key
    # as a plaintext Bearer token, and there is no per-call review that
    # would catch a stray public HTTP bind the way hub_client.py's own
    # _validate_hub_url check catches one on the client side. This is the
    # server-side mirror of that check -- refuse to start rather than silently
    # serve credentials in cleartext to the public internet. Set explicitly
    # for a deployment that intentionally terminates TLS elsewhere on the
    # same host/network without an operator ever setting `host` itself to
    # anything but loopback (e.g. a sidecar proxy on 127.0.0.1 in front of a
    # loopback-bound Hub still passes the check with this left False).
    allow_insecure_http: bool = False

    # --- CommonTrace Knowledge Base ---
    # Off is the wrong default for this flag: existing deployments that
    # never set HUB_COMMONS_ENABLED must keep the tool surface they already
    # have, so the default preserves current behavior rather than opting
    # every install into a narrower one. Set explicitly to false for a
    # deployment that must not consult any content beyond what the fleet
    # itself captured -- e.g. an internal-only offering with a hard
    # requirement of "no outside knowledge", where the point is a guarantee
    # stronger than merely "nobody happens to call commons_overlap." The
    # Knowledge Base is already opt-in per plan (`commons_access`) and holds
    # only operator-curated content (hub/manage.py:commons_seed, plus
    # accepted community submissions -- see submit_kb_entry), never a
    # customer's own traces directly -- this flag exists for deployments
    # that want the surface gone entirely rather than merely unused. False
    # removes commons_overlap, commons_search, submit_kb_entry, and
    # list_my_kb_submissions from the MCP tool surface entirely
    # (hub/server.py) -- an unknown-tool error to any client that tries,
    # not a refused call -- so the property holds even if every org on the
    # deployment forgets the Knowledge Base exists. account_usage stays
    # available either way: it reports an org's own plan and its own usage,
    # never Knowledge Base content, so disabling it does not touch that.
    commons_enabled: bool = True

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

    def validate_transport_safety(self) -> None:
        """Refuse to construct a config that would serve plaintext HTTP on a
        publicly reachable interface. Called from hub/main.py at startup --
        not from __post_init__, so tests and callers building a HubConfig
        for a loopback-bound local server (the common case in hub/tests/)
        never have to think about this."""
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
            read_rate_limit_per_minute=_env_int_in_range("HUB_READ_RATE_LIMIT_PER_MINUTE", 300, 0, 10_000_000),
            read_rate_limit_burst=_env_int_in_range("HUB_READ_RATE_LIMIT_BURST", 60, 0, 1_000_000),
            auth_attempts_per_minute=_env_int_in_range("HUB_AUTH_ATTEMPTS_PER_MINUTE", 60, 0, 10_000_000),
            auth_attempts_burst=_env_int_in_range("HUB_AUTH_ATTEMPTS_BURST", 20, 0, 1_000_000),
            readyz_rate_limit_per_minute=_env_int_in_range("HUB_READYZ_RATE_LIMIT_PER_MINUTE", 120, 0, 10_000_000),
            readyz_rate_limit_burst=_env_int_in_range("HUB_READYZ_RATE_LIMIT_BURST", 30, 0, 1_000_000),
            trusted_proxy_hops=_env_int_in_range("HUB_TRUSTED_PROXY_HOPS", 0, 0, 16),
            allow_insecure_http=_env_bool("HUB_ALLOW_INSECURE_HTTP", False),
            admin_token=os.environ.get("HUB_ADMIN_TOKEN", ""),
            operator_org_id=os.environ.get("HUB_OPERATOR_ORG_ID", ""),
            commons_enabled=_env_bool("HUB_COMMONS_ENABLED", True),
            db_pool_size=_env_int_in_range("HUB_DB_POOL_SIZE", 10, 1, 1000),
            db_max_overflow=_env_int_in_range("HUB_DB_MAX_OVERFLOW", 5, 0, 1000),
            db_pool_timeout=_env_int_in_range("HUB_DB_POOL_TIMEOUT", 30, 1, 3600),
            db_pool_recycle=_env_int_in_range("HUB_DB_POOL_RECYCLE", 1800, -1, 86_400),
            graceful_shutdown_seconds=_env_int_in_range("HUB_GRACEFUL_SHUTDOWN_SECONDS", 30, 0, 3600),
            log_level=os.environ.get("HUB_LOG_LEVEL", "INFO"),
        )
