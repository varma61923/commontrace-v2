"""Environment-driven configuration for the Hub server.

Every setting has a HUB_-prefixed env var (documented in hub/.env.example).
No secrets have defaults that are usable in production; DATABASE_URL has no
default at all so a misconfigured deploy fails loudly instead of silently
falling back to something unsafe.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field

from hub.secrets_provider import env_secret

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


def _env_cidr_list(name: str) -> tuple[str, ...]:
    """A comma-separated list of CIDR blocks, validated at startup rather
    than on the first request that happens to reach the middleware --
    the same "name the variable and the reason" policy `_env_int_in_range`
    already applies to a numeric misconfiguration."""
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

    # Which RateLimiter implementation hub/abuse.py's make_*_rate_limiter()
    # factories construct. "memory" (default) is the original in-process
    # token bucket -- exact current behavior, so an existing deployment that
    # never sets this env var is unaffected. "postgres" shares bucket state
    # in a table in this same database instead, so a horizontally-scaled
    # deployment (multiple replicas) enforces one shared limit instead of
    # N replicas each granting their own independent allowance (hub/
    # DEPLOYMENT.md section 6). Validated in __post_init__ below rather than
    # left to fail confusingly wherever a factory happens to read it.
    rate_limit_backend: str = "memory"

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

    # --- Source-address restriction (hub/server.py:IpAllowlistMiddleware) ---
    # Empty (default): every source reaches every route, unchanged from
    # before this existed. Set HUB_IP_ALLOWLIST to a comma-separated list
    # of CIDR blocks ("10.0.0.0/8,203.0.113.5/32") to refuse every OTHER
    # source with 403, on every route except /healthz and /readyz (an
    # orchestrator's liveness/readiness probes are a different population
    # than the external traffic this restricts, and blocking them turns a
    # security control into a self-inflicted outage). Resolved via the
    # same trusted_proxy_hops-aware client-address logic the rate limiters
    # already use, so a request behind a trusted reverse proxy is checked
    # against its real origin, not the proxy's own address.
    #
    # This is the half of "no IP allowlisting / private networking" that
    # is genuinely code-only. The OTHER half -- actual private networking,
    # VPC peering, a network topology this Hub is simply unreachable from
    # outside at all -- is a deployment-topology decision made by whoever
    # operates it, not something this application can decide for them; see
    # hub/DEPLOYMENT.md for that half.
    ip_allowlist: tuple[str, ...] = ()

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

    # Signing secret for the CUSTOMER console's session cookies
    # (hub/console.py). Separate from `admin_token` on purpose: that token is
    # the operator's credential, and a value that both authenticates the
    # vendor AND signs customer sessions means one leak compromises both
    # surfaces at once.
    #
    # Unset disables the console entirely -- no /app route is registered, so a
    # deployment that has not opted in has nothing to probe, exactly as
    # `admin_token` gates /admin. Failing closed rather than generating an
    # ephemeral secret is deliberate: an ephemeral one works perfectly on a
    # single process and silently signs out every user on each deploy and
    # every scale-out, which reads as a flaky product rather than as a
    # missing setting.
    console_secret: str = ""

    # --- Public disclosure (GET /disclosure, unauthenticated) ---
    # Audit §2.3/§4.4 named "unknown data residency" and "no canonical
    # product identity" as gaps only a real operator's own hosting and
    # legal-entity decisions can close -- this codebase asserts neither on
    # its own behalf, and nothing here changes that. What was missing was
    # a PLACE for an operator who HAS made those decisions to state them
    # where a procurement reviewer can actually see them, rather than
    # every deployment needing its own bespoke status page. Unset means
    # the response says exactly that -- "not disclosed by this
    # deployment's operator" -- never a guess, a default, or silence that
    # could be mistaken for "nothing to disclose."
    data_region: str = ""
    operator_legal_name: str = ""
    operator_support_contact: str = ""

    # --- Self-serve signup (hub/signup.py) ---
    # False (default): no /signup route is registered at all -- the only way
    # to create an org is still `python -m hub.manage create-org`, run by an
    # operator. Set true to let a visitor create their own free-plan org and
    # first API key with no operator involved. There is no email
    # verification behind this (this Hub has no outbound email integration
    # to build one on) -- the free plan's own limits (hub/plans.py) are the
    # blast radius bound for an uncontactable or fraudulent signup either
    # way, the same bound every free-tier evaluator already gets.
    signup_enabled: bool = False

    # --- Self-serve billing (hub/billing.py) ---
    # Empty (default) means neither the console's "Upgrade" buttons nor the
    # /billing/webhook route do anything real: hub/billing.py's own
    # StripeSettings.checkout_configured is False, and the webhook route is
    # never registered -- a deployment that has not opted into Stripe has
    # nothing new to probe. Keep `stripe_secret_key` in a secret store next
    # to HUB_DATABASE_URL; it authenticates as this account to Stripe's API.
    stripe_secret_key: str = ""
    # The signing secret Stripe's dashboard shows for the webhook endpoint
    # you register there (pointed at https://<this-hub>/billing/webhook).
    # Required for the webhook route to verify a delivery actually came from
    # Stripe rather than from anyone who can reach this endpoint -- without
    # it, an unauthenticated POST could forge a plan upgrade for any org_id.
    stripe_webhook_secret: str = ""
    # Stripe Price ids (from the Stripe dashboard, "price_..."), one per
    # billable plan (hub/plans.py:BILLABLE_PLANS). Either may be left unset
    # if this deployment only sells one paid tier; both unset disables
    # self-serve upgrades entirely regardless of the other Stripe settings.
    stripe_price_team: str = ""
    stripe_price_scale: str = ""

    # --- Value ledger signing (hub/crud.py value_delivered, commontrace/value.py) ---
    # Empty (default) means invoices `value_delivered` returns are
    # hash-chained (commontrace.value.verify_ledger) but NOT signed:
    # `signature` in the response is null and `signature_reason` says why.
    # A hash chain alone proves an invoice is internally consistent -- that
    # nobody quietly edited one line, dropped the memory that HURT, or
    # reordered to bury it -- but the chain's genesis and algorithm are both
    # public by design (that is what lets a customer reimplement
    # verify_ledger independently), so anyone with write access to wherever
    # a ledger is stored could regenerate an entire REPLACEMENT chain from
    # different figures and it would verify exactly as cleanly as the
    # original. Setting this closes that gap: every ledger is additionally
    # signed with HMAC-SHA256 under this key (commontrace.value.sign_ledger),
    # so a signature only verifies for a chain this deployment actually
    # issued, not any chain that merely follows the public rules.
    #
    # Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`,
    # keep it in a secret store next to HUB_DATABASE_URL, and set it
    # IDENTICALLY across every replica -- unlike HUB_API_KEY_PEPPER (which
    # tolerates a per-process fallback because a missing pepper only ever
    # weakens one specific timing defense), a value ledger is meant to be
    # verifiable by the customer LATER, against a signature minted by
    # whichever replica happened to serve that request; a key that silently
    # varied by process or by restart would make some invoices verify and
    # others not, for no reason the customer could see. Rotate by setting a
    # new value and treating every signature issued under the old key as
    # still valid only if you keep verifying against both -- this module
    # does not version keys, so a rotation invalidates verification of
    # already-issued invoices unless you keep the old key available
    # out-of-band for exactly that purpose.
    ledger_signing_key: str = ""

    # --- Encryption at rest (hub/encryption.py) ---
    # Empty (default): no field-level encryption. `WebhookEndpoint.url` (the
    # only column this covers -- see hub/encryption.py's module docstring
    # for why Trace content is deliberately excluded) is stored as plaintext,
    # exactly as it always was. A base64-urlsafe, 32-byte key, generated with
    # `python -m hub.manage generate-encryption-key`. Keep it in a secret
    # store next to HUB_DATABASE_URL -- unlike `ledger_signing_key`, this key
    # is symmetric and its loss (not just its leak) is unrecoverable: an
    # existing encrypted value simply stops decrypting with no other key to
    # try.
    encryption_key: str = ""
    # Comma-separated retired keys, tried in order if the current key fails
    # to decrypt a stored value -- see hub/encryption.py's "Key rotation"
    # section. Setting this without `encryption_key` is rejected: a
    # deployment with only retired keys has no key left to encrypt with.
    encryption_key_previous: str = ""

    # --- Human identity: OIDC bearer-token verification (hub/sso.py) ---
    # One trusted issuer per deployment. Unset (the default) means SSO is
    # not configured at all: `identity_provider()` returns None, the
    # middleware never attempts JWT verification, and every request is
    # governed exactly as it was before hub/models.py:User existed. This is
    # SSO ENFORCEMENT for whichever users an operator links to it
    # (`hub.manage link-sso`) -- not a login UI, not SCIM, not SAML, and
    # never a substitute for scoped API keys; see hub/README.md "Auth
    # follow-ups" for the boundary stated in full.
    oidc_issuer: str = ""
    oidc_audience: str = ""
    # A static JWKS document as JSON, for an air-gapped deployment or one
    # that rotates keys by redeploying config. Mutually exclusive in
    # practice with oidc_jwks_uri below (the static document wins if both
    # are set, since it was explicitly supplied); validated at
    # `identity_provider()` call time, not here, so a malformed value fails
    # loudly the first time it would matter rather than at import time.
    oidc_jwks: str = ""
    # Fetched and cached with a TTL (hub/sso.py:JWKSCache) rather than
    # trusted once at startup, so a signing-key rotation at the IdP is
    # picked up without a restart.
    oidc_jwks_uri: str = ""

    # --- Tenant isolation: row-level security ---
    # Postgres skips EVERY row-level-security policy for a superuser or a
    # role holding BYPASSRLS -- silently, with no error and no log line. So
    # the dangerous state is not "RLS missing", it is "RLS installed,
    # listed by pg_policies, passing an audit, and doing nothing". This
    # deployment's own docker-compose.yml shipped exactly that shape until
    # the runtime role below existed, because the Postgres image makes
    # POSTGRES_USER the cluster superuser.
    #
    # False (default) means the Hub REFUSES TO START when it positively
    # determines that state: policies exist AND the connecting role bypasses
    # them. Fail closed, because a silently-bypassed policy is worse than an
    # absent one -- it is a guarantee an operator believes in and does not
    # have. This is the same shape as `allow_insecure_http` below: unsafe by
    # configuration is allowed, but only with an explicit acknowledgment,
    # never by accident.
    #
    # Set true to acknowledge a deployment that intentionally connects as an
    # owner/superuser (the pre-existing evaluation shape) and accept that
    # tenant isolation rests entirely on hub/crud.py's own org_id predicates,
    # with no database-enforced backstop behind them.
    allow_rls_bypass: bool = False

    # The stronger, opt-in form of the same question. `allow_rls_bypass`
    # catches policies that exist but cannot bite; it says nothing about
    # policies that are not there at all (a database built by
    # `Base.metadata.create_all` rather than by alembic, a deployment that
    # has not migrated yet, or a policy somebody dropped). Set true and the
    # Hub additionally refuses to start unless RLS is affirmatively
    # enforced -- for a deployment that wants the backstop as a checked
    # precondition rather than as something it hopes is still installed.
    require_rls: bool = False

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
    # Bounds on how much work one replica will accept before shedding it,
    # rather than queueing it until everything times out together.
    # hub/bench_concurrency.py measured the shape being bounded here: at
    # 128 concurrent clients p99 reached 1.3s against an EMPTY handler,
    # with nothing anywhere to stop it growing further.
    #
    # statement_timeout is Postgres-side, so it also covers a query whose
    # client has already given up -- the one case an application-side
    # timeout cannot reach, and the one that keeps a runaway query burning
    # a connection nobody is waiting for.
    db_statement_timeout_ms: int = 30_000
    # Application-side ceiling on one request. Shorter than the pool's own
    # 30s wait would be self-defeating (a request would expire while
    # queued for a connection it was about to get), so this is the pool
    # timeout plus the statement timeout, rounded up: the longest a
    # legitimately slow request can take before something is wrong.
    request_timeout_seconds: int = 60
    # In-flight requests per replica before new ones are refused outright.
    # A refusal with Retry-After is strictly kinder than unbounded
    # queueing: the caller learns immediately and can back off, instead of
    # waiting out a timeout to discover the same thing. 0 disables the cap.
    max_concurrent_requests: int = 512
    db_pool_recycle: int = 1800     # recycle connections older than 30 min

    # --- Lifecycle ---
    graceful_shutdown_seconds: int = 30

    # --- Alert scheduler (hub/scheduler.py) ---
    # False (default): no background loop starts at all -- a deployment
    # already pointing cron at `hub.manage check-alerts` sees no change.
    # True: the Hub process sweeps hub/alerts.py's check_rules itself,
    # every alert_scheduler_interval_seconds, for as long as the process
    # runs. See hub/scheduler.py's own docstring for why running this on
    # more than one replica is safe.
    alert_scheduler_enabled: bool = False
    alert_scheduler_interval_seconds: int = 300

    # --- Misc ---
    log_level: str = "INFO"
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.rate_limit_backend not in ("memory", "postgres"):
            raise ValueError(
                f"HUB_RATE_LIMIT_BACKEND must be 'memory' or 'postgres', got {self.rate_limit_backend!r}"
            )
        # Validated eagerly, at construction, rather than on whichever
        # webhook registration or delivery attempt happens to be the first
        # to need it -- the same "fail loud, name the reason" policy this
        # module applies to every other setting.
        self.cipher()

    def cipher(self):
        """The at-rest cipher for fields safe to encrypt (see
        hub/encryption.py's module docstring for which fields, and why
        Trace content is not among them). Built fresh each call, like
        `identity_provider()` below -- a caller that wants it built once
        holds the result itself."""
        from hub.encryption import EnvelopeCipher

        return EnvelopeCipher.from_config(self.encryption_key, self.encryption_key_previous)

    def identity_provider(self):
        """The trusted OIDC issuer this deployment verifies bearer tokens
        against, or None when SSO is not configured at all.

        Built once and reused (hub/server.py holds the result rather than
        calling this per request) -- not for the cost of building it, which
        is trivial, but so a single malformed `HUB_OIDC_JWKS` value fails
        at startup, loud and once, rather than on whichever request happens
        to trigger the first JWT verification attempt.
        """
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
        # env_secret, not a plain os.environ.get: every setting below that
        # is an actual secret (as opposed to operational config like
        # HUB_HOST or a rate limit) additionally honors a `{NAME}_FILE`
        # variable naming a file to read the value from instead -- see
        # hub/secrets_provider.py for why this, rather than one vendor's
        # SDK, is what lets a real secret store (Vault, any cloud
        # provider's Secrets Store CSI driver, Kubernetes Secret volumes,
        # Docker secrets) supply these values with no further code here.
        database_url = env_secret("HUB_DATABASE_URL")
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
            encryption_key=env_secret("HUB_ENCRYPTION_KEY"),
            encryption_key_previous=env_secret("HUB_ENCRYPTION_KEY_PREVIOUS"),
            admin_token=env_secret("HUB_ADMIN_TOKEN"),
            operator_org_id=os.environ.get("HUB_OPERATOR_ORG_ID", ""),
            console_secret=env_secret("HUB_CONSOLE_SECRET"),
            data_region=os.environ.get("HUB_DATA_REGION", ""),
            operator_legal_name=os.environ.get("HUB_OPERATOR_LEGAL_NAME", ""),
            operator_support_contact=os.environ.get("HUB_OPERATOR_SUPPORT_CONTACT", ""),
            signup_enabled=_env_bool("HUB_SIGNUP_ENABLED", False),
            stripe_secret_key=env_secret("HUB_STRIPE_SECRET_KEY"),
            stripe_webhook_secret=env_secret("HUB_STRIPE_WEBHOOK_SECRET"),
            # Price ids ("price_...") are references, not credentials --
            # safe to read directly, same as operator_org_id above.
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
            log_level=os.environ.get("HUB_LOG_LEVEL", "INFO"),
        )
