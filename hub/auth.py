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
from datetime import datetime, timezone

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


def generate_raw_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


async def issue_api_key(session: AsyncSession, org_id: str) -> IssuedKey:
    org = await session.get(Organization, org_id)
    if org is None:
        raise ValueError(f"no such organization: {org_id}")

    raw_key = generate_raw_key()
    key_hash = _hasher.hash(raw_key)
    api_key = ApiKey(org_id=org_id, key_prefix=raw_key[:_PREFIX_LEN], key_hash=key_hash)
    session.add(api_key)
    await session.flush()
    return IssuedKey(key_id=api_key.id, org_id=org_id, raw_key=raw_key)


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
    return await issue_api_key(session, old.org_id)


async def revoke_api_key(session: AsyncSession, key_id: str) -> None:
    await session.execute(
        update(ApiKey).where(ApiKey.id == key_id).values(revoked_at=datetime.now(timezone.utc))
    )


async def verify_api_key(session: AsyncSession, raw_key: str) -> str | None:
    """Return the org_id the key belongs to, or None if invalid/revoked.
    Never raises on a bad key -- an unrecognized or malformed key is simply
    "not authenticated", not a server error."""
    if not raw_key or not raw_key.startswith(_KEY_PREFIX):
        return None
    prefix = raw_key[:_PREFIX_LEN]
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
        candidate.last_used_at = datetime.now(timezone.utc)
        return candidate.org_id
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


def get_current_org_id() -> str:
    org_id = current_org_id.get()
    if org_id is None:
        raise PermissionError("no authenticated organization in request context")
    return org_id
