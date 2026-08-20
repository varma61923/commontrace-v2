"""API-key-per-org authentication (MVP; OAuth/JWT is an explicit follow-up,
not implemented here -- see hub/README.md "Auth follow-ups").

Design:
  - A raw key looks like `ct_live_<43 url-safe base64 chars>` (~32 bytes of
    entropy). It is shown to the operator exactly once, at issuance, and
    never stored or logged in recoverable form.
  - Only an argon2id hash of the raw key is persisted (hub/models.py
    ApiKey.key_hash), plus a non-secret `key_prefix` (first 12 chars of the
    raw key) so an operator can identify *which* key a log line or DB row
    refers to without ever being able to reconstruct the secret from it.
  - Verification hashes the presented key and looks it up by prefix first
    (indexed, cheap) then confirms the full hash with argon2's constant-time
    verify -- never a linear scan comparing raw strings.
"""

from __future__ import annotations

import contextvars
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hub.models import ApiKey, Organization

_KEY_PREFIX = "ct_live_"
_PREFIX_LEN = 12  # "ct_live_" + 4 chars, enough to disambiguate without leaking useful entropy

_hasher = PasswordHasher()


@dataclass(frozen=True)
class IssuedKey:
    key_id: str
    org_id: str
    raw_key: str  # only ever available at issuance time; caller must display/store it now
    key_prefix: str = ""  # non-secret; safe to log / use as an audit actor


def generate_raw_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


async def issue_api_key(session: AsyncSession, org_id: str, expires_days: int | None = None) -> IssuedKey:
    """`expires_days=None` (the default) issues a non-expiring key, matching
    the behavior before expiry existed. A positive value sets `expires_at`,
    after which verify_api_key rejects the key with no revocation job
    needing to run."""
    org = await session.get(Organization, org_id)
    if org is None:
        raise ValueError(f"no such organization: {org_id}")

    expires_at = None
    if expires_days is not None:
        if expires_days <= 0:
            raise ValueError(f"expires_days must be positive, got {expires_days}")
        expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)

    raw_key = generate_raw_key()
    key_hash = _hasher.hash(raw_key)
    api_key = ApiKey(
        org_id=org_id, key_prefix=raw_key[:_PREFIX_LEN], key_hash=key_hash, expires_at=expires_at
    )
    session.add(api_key)
    await session.flush()
    return IssuedKey(key_id=api_key.id, org_id=org_id, raw_key=raw_key, key_prefix=api_key.key_prefix)


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


@dataclass(frozen=True)
class AuthenticatedKey:
    org_id: str
    key_prefix: str  # non-secret; used to attribute audit-log entries


async def verify_api_key(session: AsyncSession, raw_key: str) -> AuthenticatedKey | None:
    """Return the authenticated org (plus the key's non-secret prefix, for
    audit attribution), or None if the key is invalid, revoked, or expired.
    Never raises on a bad key -- an unrecognized or malformed key is simply
    "not authenticated", not a server error."""
    if not raw_key or not raw_key.startswith(_KEY_PREFIX):
        return None
    prefix = raw_key[:_PREFIX_LEN]
    now = datetime.now(timezone.utc)
    candidates = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_prefix == prefix, ApiKey.revoked_at.is_(None))
        )
    ).scalars().all()
    for candidate in candidates:
        try:
            _hasher.verify(candidate.key_hash, raw_key)
        except VerifyMismatchError:
            continue
        if candidate.expires_at is not None and candidate.expires_at <= now:
            # Expired reads exactly like invalid: an expired key must not be
            # distinguishable from a wrong one at the transport layer.
            return None
        candidate.last_used_at = now
        return AuthenticatedKey(org_id=candidate.org_id, key_prefix=candidate.key_prefix)
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


def get_current_org_id() -> str:
    org_id = current_org_id.get()
    if org_id is None:
        raise PermissionError("no authenticated organization in request context")
    return org_id


def get_current_actor() -> str:
    from hub.audit import actor_for_api_key

    prefix = current_actor.get()
    return actor_for_api_key(prefix) if prefix else "unknown"
