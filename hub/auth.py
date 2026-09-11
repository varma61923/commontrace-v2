"""API-key-per-org authentication (MVP; OAuth/JWT is an explicit follow-up,
not implemented here -- see hub/README.md "Auth follow-ups").

Design:
  - A raw key looks like `ct_live_<43 url-safe base64 chars>` (~32 bytes of
    entropy). It is shown to the operator exactly once, at issuance, and
    never stored or logged in recoverable form.
  - Two independent digests of the raw key are persisted (hub/models.py
    ApiKey), plus a non-secret `key_prefix` (first 12 chars of the raw key)
    so an operator can identify *which* key a log line or DB row refers to
    without ever being able to reconstruct the secret from it:

      - `key_hmac`: HMAC-SHA256(pepper, raw_key), hex. THE VERIFICATION
        PATH -- an indexed exact-match lookup, checked first on every
        request.
      - `key_hash`: an argon2id hash, kept as a fallback and break-glass
        copy (see WHY ARGON2 IS STILL HERE, below), checked only when the
        HMAC lookup misses.

WHY HMAC, NOT ARGON2, FOR VERIFICATION. Argon2id exists to make guessing a
LOW-ENTROPY human password expensive. `generate_raw_key` below mints 256
bits from `secrets` -- there is nothing to brute-force, so Argon2's cost
bought no security at all, on the product's highest-traffic code path.
Measured on this machine, end to end including the DB round trip each way
(argon2-cffi default params: t=3, m=64MiB, p=4):

    legacy (Argon2) verify_api_key:  63.9 ms median
    fast (key_hmac) verify_api_key:   1.1 ms median
    speedup:                         56.5x

which on a 4-core box is the difference between an authentication ceiling
around 48 req/s (the whole Hub, since every authenticated request paid it)
and one in the thousands/s -- at which point something else (the connection
pool, the read-rate limiter) becomes the binding constraint instead. An
HMAC lookup is also, being an ordinary indexed equality query, the same
cost whether the row exists or not -- so it does not reopen the timing
question below, it makes it moot for any key that has one.

WHY ARGON2 IS STILL HERE. Raw keys are one-way hashed and never recoverable,
so there is no way to compute `key_hmac` for a key that predates this
column except by seeing that key presented again. `verify_api_key` therefore
tries the fast HMAC path first and, on a miss, falls through to the ORIGINAL
prefix-scan-plus-Argon2 verification unchanged -- so an existing key keeps
working with no migration window -- and backfills `key_hmac` the moment that
legacy path succeeds, so the fast path covers it from then on. `key_hash`
itself is never deleted: it is the only fallback if `HUB_API_KEY_PEPPER` is
ever lost or rotated, since a lost pepper makes every `key_hmac` unverifiable
at once (rotate by re-peppering and letting the fallback path re-backfill).
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hub import scopes as scopes_module
from hub.models import ApiKey, Organization

_KEY_PREFIX = "ct_live_"
_PREFIX_LEN = 12  # "ct_live_" + 4 chars, enough to disambiguate without leaking useful entropy

_hasher = PasswordHasher()

# A valid argon2id hash of a value that is never a real key. The LEGACY
# fallback path (only reached when the HMAC lookup below misses) runs this
# through the same verify() call a real candidate would get whenever no
# key_prefix matches the presented key at all -- without it, "no such
# prefix" returns instantly while "prefix exists but the rest of the key is
# wrong" pays for a full argon2id computation (tens of milliseconds). That
# timing gap lets a remote attacker distinguish the two cases without ever
# guessing a real key: enough responses timed against enough presented
# prefixes reveals which key_prefix values exist in the database at all,
# i.e. which orgs/keys exist, before any brute-forcing of the actual secret
# begins. This defense is specific to the prefix-scan shape of the legacy
# path; the HMAC path has no "candidate rows sharing a prefix" concept to
# leak in the first place (see verify_api_key).
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))

# HMAC-SHA256(pepper, raw_key) is the verification key, not a secret an
# attacker who reads it out of the database could use alone -- reversing an
# HMAC digest is as hard as reversing SHA-256 itself, pepper or not. The
# pepper's job is narrower: it stops someone who has ONLY read `key_hmac`
# values (a DB dump, a backup, a read replica) from computing their own
# digest of a guessed key and checking it against those values offline,
# which a bare unkeyed hash of the key would allow.
#
# Set HUB_API_KEY_PEPPER (any string, kept identical across every replica
# and every restart of a given deployment) for the fast path's benefit to
# survive a restart and to be shared across replicas. Left unset, a random
# pepper is generated per process: HMAC verification still works, correctly,
# for any key backfilled within THIS process's own lifetime, but a restart
# or a different replica will not recognize digests this one computed, and
# such requests simply fall through to the always-correct legacy path below
# -- a throughput regression for that key on that request, never a security
# or correctness one.
_pepper_env = os.environ.get("HUB_API_KEY_PEPPER", "")
_PEPPER = _pepper_env.encode("utf-8") if _pepper_env else secrets.token_bytes(32)


def _key_hmac(raw_key: str) -> str:
    return hmac.new(_PEPPER, raw_key.encode("utf-8"), hashlib.sha256).hexdigest()

# How stale last_used_at may be before verify_api_key bothers to refresh it.
# See the write site below for why this exists: idle-key auditing needs
# roughly-current information, not per-request precision.
_LAST_USED_AT_UPDATE_INTERVAL = timedelta(minutes=5)


@dataclass(frozen=True)
class IssuedKey:
    key_id: str
    org_id: str
    raw_key: str  # only ever available at issuance time; caller must display/store it now
    key_prefix: str = ""  # non-secret; safe to log / use as an audit actor
    # What the issued key may do (hub/scopes.py). Returned so the caller can
    # print it: an operator who asked for a narrow key needs to see what they
    # actually got, and one who asked for nothing needs to see that the
    # default is everything.
    scopes: tuple[str, ...] = ()


def generate_raw_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


async def issue_api_key(
    session: AsyncSession,
    org_id: str,
    expires_days: int | None = None,
    scopes: str | list | tuple | None = None,
) -> IssuedKey:
    """`expires_days=None` (the default) issues a non-expiring key, matching
    the behavior before expiry existed. A positive value sets `expires_at`,
    after which verify_api_key rejects the key with no revocation job
    needing to run.

    `scopes=None` grants every scope -- what a key could do before scopes
    existed, so the documented one-liner onboarding is unchanged. Pass a
    subset ("read", ["read", "write"]) for a least-privilege workload token;
    hub/scopes.py raises on an unknown scope rather than silently dropping
    it, because a typo that quietly narrows or widens a credential is a
    privilege change nobody reviews.
    """
    org = await session.get(Organization, org_id)
    if org is None:
        raise ValueError(f"no such organization: {org_id}")

    # Validated before any expensive work and before the row exists: a
    # ScopeError here must not leave a half-issued key behind.
    granted_scopes = scopes_module.parse(scopes)

    expires_at = None
    if expires_days is not None:
        if expires_days <= 0:
            raise ValueError(f"expires_days must be positive, got {expires_days}")
        expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)

    raw_key = generate_raw_key()
    # argon2id hashing is deliberately expensive (that is the whole point of
    # using it) -- tens of milliseconds of pure CPU work. Called inline on
    # the request coroutine, that blocks THIS event loop, and with it every
    # other request the single-process Hub is concurrently serving, not just
    # the one issuing a key. asyncio.to_thread moves it off the loop onto a
    # worker thread so issuance stays expensive only for its own caller.
    # Issuance is rare (an operator action, not a per-request cost), so
    # keeping this computation -- unlike verification -- is simply the
    # cheapest way to keep key_hash populated for every row, uniformly, with
    # no "some rows have it, some don't" special case anywhere downstream.
    key_hash = await asyncio.to_thread(_hasher.hash, raw_key)
    api_key = ApiKey(
        org_id=org_id, key_prefix=raw_key[:_PREFIX_LEN], key_hash=key_hash,
        key_hmac=_key_hmac(raw_key), expires_at=expires_at,
        scopes=list(granted_scopes),
    )
    session.add(api_key)
    await session.flush()
    return IssuedKey(
        key_id=api_key.id, org_id=org_id, raw_key=raw_key,
        key_prefix=api_key.key_prefix, scopes=granted_scopes,
    )


async def rotate_api_key(session: AsyncSession, old_key_id: str) -> IssuedKey:
    """Revoke `old_key_id` and issue a fresh key for the same org, atomically
    within the caller's transaction. The old key stops verifying the moment
    this commits; nothing in between grants a window with two live keys
    unless the caller wants that (call issue_api_key again before revoking
    if a rollover grace period is desired)."""
    old = await session.get(ApiKey, old_key_id)
    if old is None:
        raise ValueError(f"no such api key: {old_key_id}")
    old.revoked_at = datetime.now(timezone.utc)
    # Carry the old key's expiry *policy* forward: a key that was issued to
    # expire in 90 days rotates into another 90-day key, rather than
    # silently becoming a non-expiring one.
    expires_days = None
    if old.expires_at is not None:
        span = old.expires_at - old.created_at
        expires_days = max(1, round(span.total_seconds() / 86400))
    return await issue_api_key(session, old.org_id, expires_days=expires_days)


async def revoke_api_key(session: AsyncSession, key_id: str) -> None:
    await session.execute(
        update(ApiKey).where(ApiKey.id == key_id).values(revoked_at=datetime.now(timezone.utc))
    )


def _scopes_of(raw: object) -> tuple[str, ...] | None:
    """Normalize the `scopes` column into what AuthenticatedKey carries.

    NULL means a key that predates the column -- full capability, see
    hub/scopes.py. Anything else, including an empty array, is taken
    literally: a deliberately narrowed key.
    """
    if raw is None:
        return None
    return tuple(raw)


@dataclass(frozen=True)
class AuthenticatedKey:
    org_id: str
    key_prefix: str  # non-secret; used to attribute audit-log entries
    # What this key may do (hub/scopes.py). None means a key issued before
    # the column existed, which held every capability at issuance -- see
    # scopes.satisfies for why that is distinct from an empty tuple.
    scopes: tuple[str, ...] | None = None


async def verify_api_key(session: AsyncSession, raw_key: str) -> AuthenticatedKey | None:
    """Return the authenticated org (plus the key's non-secret prefix, for
    audit attribution), or None if the key is invalid, revoked, or expired.
    Never raises on a bad key -- an unrecognized or malformed key is simply
    "not authenticated", not a server error.

    Tries the fast `key_hmac` lookup first; a miss falls through to the
    original prefix-scan-plus-Argon2 path unchanged (`_verify_by_legacy_scan`),
    which backfills `key_hmac` on success so the fast path covers this key
    from its next request on. See this module's docstring for why both
    exist and why that order is safe.
    """
    if not raw_key or not raw_key.startswith(_KEY_PREFIX):
        return None
    now = datetime.now(timezone.utc)

    fast = await _verify_by_hmac(session, raw_key, now)
    if fast is not None:
        return fast
    return await _verify_by_legacy_scan(session, raw_key, now)


async def _verify_by_hmac(session: AsyncSession, raw_key: str, now: datetime) -> AuthenticatedKey | None:
    """O(1) via the unique index on `key_hmac` -- no Argon2 call, on either
    a hit or a miss. A plain equality lookup costs the same (modulo index
    depth, which carries no information about the presented secret) whether
    it matches or not, so this path has no timing gap for the _DUMMY_HASH
    defense above to close -- there is no "candidate rows share this
    prefix" fact left to leak, because nothing here is grouped by prefix.

    A raw Core column select, not `select(ApiKey)`: this runs on every
    authenticated request, and hydrating a full mapped entity (identity map,
    instrumented attributes) for the handful of columns actually needed here
    is pure overhead on the hottest path in the process.
    """
    row = (
        await session.execute(
            select(
                ApiKey.id, ApiKey.org_id, ApiKey.key_prefix, ApiKey.key_hmac,
                ApiKey.revoked_at, ApiKey.expires_at, ApiKey.last_used_at,
                ApiKey.scopes,
            ).where(ApiKey.key_hmac == _key_hmac(raw_key))
        )
    ).first()
    if row is None:
        return None
    # Belt and braces: the SQL equality above already did the real
    # filtering, but compare_digest costs nothing extra and removes any
    # doubt that a future change to this query could reintroduce a
    # non-constant-time comparison of secret material.
    if not hmac.compare_digest(row.key_hmac, _key_hmac(raw_key)):
        return None
    if row.revoked_at is not None:
        return None
    if row.expires_at is not None and row.expires_at <= now:
        return None
    # Same throttling rationale as the legacy path below: idle-key auditing
    # needs roughly-current information, not per-request precision, and an
    # unconditional write here would serialize a hot key's concurrent
    # requests against each other's row lock for no operational benefit.
    if row.last_used_at is None or (now - row.last_used_at) >= _LAST_USED_AT_UPDATE_INTERVAL:
        await session.execute(update(ApiKey).where(ApiKey.id == row.id).values(last_used_at=now))
    return AuthenticatedKey(
        org_id=row.org_id, key_prefix=row.key_prefix, scopes=_scopes_of(row.scopes)
    )


async def _verify_by_legacy_scan(session: AsyncSession, raw_key: str, now: datetime) -> AuthenticatedKey | None:
    """The ORIGINAL verify_api_key, unchanged, for a key with no `key_hmac`
    yet (issued before that column existed) or one whose digest was computed
    under a different process's ephemeral pepper (see _PEPPER above). Only
    reached when `_verify_by_hmac` already missed, so this is off the hot
    path for any key that has completed one round through it.
    """
    prefix = raw_key[:_PREFIX_LEN]
    candidates = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_prefix == prefix, ApiKey.revoked_at.is_(None))
        )
    ).scalars().all()
    if not candidates:
        # Burn the same argon2id cost a real verification attempt would pay,
        # so "no matching prefix" is not distinguishable by response timing
        # from "prefix matched, full key didn't" -- see _DUMMY_HASH above.
        try:
            await asyncio.to_thread(_hasher.verify, _DUMMY_HASH, raw_key)
        except VerifyMismatchError:
            pass
        return None
    for candidate in candidates:
        try:
            # Same reasoning as issue_api_key's to_thread: verify() is the
            # same expensive argon2id computation run in reverse, and this
            # path runs on EVERY authenticated request -- inline, it would
            # stall the event loop (and every concurrent request) on every
            # single call, not just at key-issuance time.
            await asyncio.to_thread(_hasher.verify, candidate.key_hash, raw_key)
        except VerifyMismatchError:
            continue
        except InvalidHashError:
            # candidate.key_hash isn't a well-formed argon2 hash string --
            # DB corruption, a hand-edited row, or a hash written by a
            # different scheme entirely. InvalidHashError is a ValueError
            # subclass, not VerificationError, so it was previously
            # unhandled here: it escaped verify_api_key, through the auth
            # middleware, as an unhandled exception -- turning "this one
            # row is corrupt" into an HTTP 500 for every request presenting
            # a key sharing that row's prefix, other valid candidates
            # included. Treated the same as a mismatch: this row can never
            # authenticate, so move on to the next candidate rather than
            # failing the whole lookup.
            continue
        # Re-read revocation state fresh rather than trusting `candidate`
        # (loaded by the `revoked_at IS NULL` SELECT above, before the
        # possibly-slow verify() this loop just spent its time in). An
        # operator's revoke_api_key landing in that window would otherwise
        # still authenticate this request: the initial SELECT already
        # passed, and `candidate` in memory has no way to see a commit that
        # happened after it was loaded. A plain column SELECT (not
        # session.get, which would just return the same identity-mapped
        # object already held in memory) forces an actual round trip, which
        # shrinks the race window down to the time between this read and
        # the caller using its result, instead of the full duration of
        # verify(). Still not a hard guarantee under extreme scheduling, but
        # closing a multi-millisecond window to a near-zero one is the
        # practical fix short of locking the key row for every read.
        revoked_at = await session.scalar(select(ApiKey.revoked_at).where(ApiKey.id == candidate.id))
        if revoked_at is not None:
            return None
        if candidate.expires_at is not None and candidate.expires_at <= now:
            # Expired reads exactly like invalid: an expired key must not be
            # distinguishable from a wrong one at the transport layer.
            return None
        # Throttled, not written on every call: last_used_at exists for
        # idle-key auditing (hub/manage.py's key listing), which needs
        # roughly-current information, not per-request precision. Writing
        # it unconditionally means a hot key under real production QPS
        # issues an UPDATE against its own single row on every single
        # authenticated request -- every one of those write transactions
        # briefly locks the same row, so a busy key serializes concurrent
        # requests against each other for no operational benefit. Skipping
        # the write when the existing value is already within the
        # interval keeps the column meaningfully fresh while cutting write
        # volume by roughly the same factor as the interval.
        if candidate.last_used_at is None or (now - candidate.last_used_at) >= _LAST_USED_AT_UPDATE_INTERVAL:
            candidate.last_used_at = now
        # Backfill the fast path for next time. Deterministic in `raw_key`,
        # so two requests racing this same key at once (both missing the
        # fast lookup because neither has backfilled yet) both compute the
        # SAME digest and write it to the SAME row -- an idempotent update,
        # not a unique-constraint race, even though key_hmac is unique
        # across DIFFERENT rows.
        if candidate.key_hmac is None:
            candidate.key_hmac = _key_hmac(raw_key)
        return AuthenticatedKey(
            org_id=candidate.org_id, key_prefix=candidate.key_prefix,
            scopes=_scopes_of(candidate.scopes),
        )
    return None


# --- Request-scoped org identity -------------------------------------------
#
# The MCP SDK's built-in auth machinery (mcp.server.auth) is OAuth-shaped
# (issuer_url, resource metadata, token introspection) -- overkill for the
# API-key-per-org MVP the brief asks for, and the brief explicitly defers
# OAuth/JWT to a follow-up. Instead of forcing API keys through that OAuth
# surface, current_org_id is a plain contextvar set by a small Starlette
# middleware (hub/server.py: ApiKeyAuthMiddleware) that authenticates the
# request *before* it reaches MCP tool dispatch, and rejects unauthenticated
# requests with a 401 there -- tool handlers only ever run with a resolved
# org_id already in context.

current_org_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_org_id", default=None)

# Set alongside current_org_id by the same middleware. Carries the
# authenticated key's non-secret prefix so mutating tool calls can attribute
# their audit-log rows (hub/audit.py) without re-reading the key.
current_actor: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_actor", default=None)

# Set alongside the two above by the same middleware: what the authenticated
# key is allowed to do (hub/scopes.py). None means either "no authenticated
# request in context" (operator CLI, benchmarks) or "a key issued before
# scopes existed" -- both of which `require_scope` treats as unrestricted,
# and for the same reason it always has: those paths were never gated by a
# key's capabilities in the first place.
current_scopes: contextvars.ContextVar[tuple[str, ...] | None] = contextvars.ContextVar(
    "current_scopes", default=None
)


class ScopeDenied(PermissionError):
    """The key authenticated, and is not allowed to do this.

    A PermissionError subclass so any handler that only knows about that
    base class still refuses correctly, but hub/server.py's
    `_error_response` gives it its own label ("forbidden", not
    "unauthorized"): the credential is valid, and telling a caller to
    re-authenticate when retrying with the same key will fail identically
    forever is a lie that turns a configuration error into a retry loop.

    `required` and `granted` are carried as attributes so the wire response
    can be acted on without parsing the message.
    """

    def __init__(self, message: str, required: str, granted: tuple[str, ...] | None):
        super().__init__(message)
        self.required = required
        self.granted = granted


def require_scope(required: str) -> None:
    """Raise `ScopeDenied` unless the key in context holds `required`.

    Called from one place per tool (hub/server.py's `_scoped_tool`), so the
    enforcement point is the registration site an author cannot forget to
    write -- a tool registered with no scope does not compile past
    `_scoped_tool`, and hub/tests asserts every registered tool declares one.
    """
    from hub import scopes as scopes_module

    granted = current_scopes.get()
    if scopes_module.satisfies(granted, required):
        return
    raise ScopeDenied(
        f"this API key does not have the {required!r} scope "
        f"(it holds: {scopes_module.describe(granted)}). Issue a key that does with "
        f"`python -m hub.manage issue-key <org_id> <days> {required}` -- "
        "re-authenticating with the same key will not help.",
        required=required,
        granted=granted,
    )


def get_current_org_id() -> str:
    org_id = current_org_id.get()
    if org_id is None:
        raise PermissionError("no authenticated organization in request context")
    return org_id


def get_current_actor() -> str:
    from hub.audit import actor_for_api_key

    prefix = current_actor.get()
    return actor_for_api_key(prefix) if prefix else "unknown"
