from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerifyMismatchError

    _has_argon2 = True
except ImportError:
    PasswordHasher = None  # type: ignore[assignment, misc]

    class InvalidHashError(ValueError):  # type: ignore[no-redef]
        """Fallback stub when argon2-cffi is not installed."""

    class VerifyMismatchError(Exception):  # type: ignore[no-redef]
        """Fallback stub when argon2-cffi is not installed."""

    _has_argon2 = False

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hub import scopes as scopes_module
from hub.models import ApiKey, Organization, User
from hub.secrets_provider import env_secret

logger = logging.getLogger(__name__)

_KEY_PREFIX = "ct_live_"
_PREFIX_LEN = 12

if _has_argon2 and PasswordHasher is not None:
    _hasher: PasswordHasher | None = PasswordHasher()
    _DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))
else:
    _hasher = None
    _DUMMY_HASH = "$argon2id$v=19$m=65536,t=3,p=4$dummy$dummy"

_pepper_env = env_secret("HUB_API_KEY_PEPPER")
_PEPPER = _pepper_env.encode("utf-8") if _pepper_env else secrets.token_bytes(32)


def _key_hmac(raw_key: str) -> str:
    return hmac.new(_PEPPER, raw_key.encode("utf-8"), hashlib.sha256).hexdigest()


def _hash_argon2(secret: str) -> str:
    if not _has_argon2 or PasswordHasher is None or _hasher is None:
        raise RuntimeError(
            "argon2-cffi is required for API key legacy hashing/issuance. Install with: pip install argon2-cffi"
        )
    return _hasher.hash(secret)


def _verify_argon2(secret: str, stored_hash: str) -> bool:
    if not _has_argon2 or PasswordHasher is None or _hasher is None:
        logger.warning("argon2-cffi is not installed; cannot verify legacy argon2 hash")
        return False
    try:
        return bool(_hasher.verify(stored_hash, secret))
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False

_LAST_USED_AT_UPDATE_INTERVAL = timedelta(minutes=5)


@dataclass(frozen=True)
class IssuedKey:
    key_id: str
    org_id: str
    raw_key: str
    key_prefix: str = ""
    scopes: tuple[str, ...] = ()


def generate_raw_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


async def issue_api_key(
    session: AsyncSession,
    org_id: str,
    expires_days: int | None = None,
    scopes: str | list | tuple | None = None,
) -> IssuedKey:
    org = await session.get(Organization, org_id)
    if org is None:
        raise ValueError(f"no such organization: {org_id}")

    granted_scopes = scopes_module.parse(scopes)

    expires_at = None
    if expires_days is not None:
        if expires_days <= 0:
            raise ValueError(f"expires_days must be positive, got {expires_days}")
        expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)

    raw_key = generate_raw_key()
    if not _has_argon2 or PasswordHasher is None or _hasher is None:
        raise RuntimeError(
            "argon2-cffi is required for API key issuance. Install with: pip install argon2-cffi"
        )
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
    old = (await session.execute(
        select(ApiKey).where(ApiKey.id == old_key_id).with_for_update()
    )).scalar_one_or_none()
    if old is None:
        raise ValueError(f"no such api key: {old_key_id}")
    if old.revoked_at is not None:
        raise ValueError(f"api key {old_key_id} is already revoked; issue a new key instead")
    if not old.scopes:
        raise ValueError(f"api key {old_key_id} holds no scopes, so it has nothing to carry forward")
    old.revoked_at = datetime.now(timezone.utc)
    await announce_auth_change(session)
    expires_days = None
    if old.expires_at is not None:
        span = old.expires_at - old.created_at
        expires_days = max(1, round(span.total_seconds() / 86400))
    return await issue_api_key(
        session, old.org_id, expires_days=expires_days, scopes=list(old.scopes),
    )


async def revoke_api_key(session: AsyncSession, key_id: str) -> None:
    await announce_auth_change(session)
    await session.execute(
        update(ApiKey).where(ApiKey.id == key_id).values(revoked_at=datetime.now(timezone.utc))
    )


def _scopes_of(raw: object) -> tuple[str, ...] | None:
    if raw is None:
        return None
    return tuple(raw)


@dataclass(frozen=True)
class AuthenticatedKey:
    org_id: str
    key_prefix: str
    scopes: tuple[str, ...] | None = None
    expires_at: datetime | None = field(default=None, compare=False, repr=False)


_AUTH_CACHE_TTL = 0.0
_AUTH_CACHE_MAX = 4096
_AUTH_CACHE: dict[str, tuple[float, "AuthenticatedKey"]] = {}
_AUTH_CACHE_REQUIRES_LISTENER = False
_AUTH_CACHE_LISTENER_LIVE = False


def configure_auth_cache(seconds: float, *, require_listener: bool = False) -> None:
    global _AUTH_CACHE_TTL, _AUTH_CACHE_REQUIRES_LISTENER
    _AUTH_CACHE_TTL = max(0.0, min(float(seconds), 60.0))
    _AUTH_CACHE_REQUIRES_LISTENER = require_listener
    _AUTH_CACHE.clear()


def set_auth_listener_live(live: bool) -> None:
    """Called by hub/auth_invalidation.py as its connection comes and goes."""
    global _AUTH_CACHE_LISTENER_LIVE
    _AUTH_CACHE_LISTENER_LIVE = live
    _AUTH_CACHE.clear()


def _cache_usable() -> bool:
    if _AUTH_CACHE_TTL <= 0:
        return False
    return _AUTH_CACHE_LISTENER_LIVE or not _AUTH_CACHE_REQUIRES_LISTENER


def clear_auth_cache() -> None:
    _AUTH_CACHE.clear()


AUTH_INVALIDATION_CHANNEL = "commontrace_auth_invalidate"


async def announce_auth_change(session: AsyncSession) -> None:
    clear_auth_cache()
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        from sqlalchemy import text

        await session.execute(text("SELECT pg_notify(:channel, '')"), {"channel": AUTH_INVALIDATION_CHANNEL})


def cached_key(raw_key: str) -> "AuthenticatedKey | None":
    """A verified key remembered from the last few seconds, or None. Costs no database access."""
    if not raw_key or not _cache_usable():
        return None
    entry = _AUTH_CACHE.get(_key_hmac(raw_key))
    if entry is None:
        return None
    if time.monotonic() >= entry[0]:
        _AUTH_CACHE.pop(_key_hmac(raw_key), None)
        return None
    return entry[1]


def _remember(raw_key: str, key: "AuthenticatedKey") -> None:
    if not _cache_usable():
        return
    window = _AUTH_CACHE_TTL
    if key.expires_at is not None:
        window = min(window, (key.expires_at - datetime.now(timezone.utc)).total_seconds())
        if window <= 0:
            return
    if len(_AUTH_CACHE) >= _AUTH_CACHE_MAX:
        _AUTH_CACHE.clear()
    _AUTH_CACHE[_key_hmac(raw_key)] = (time.monotonic() + window, key)


_DEPLOYMENT_REGION = ""


def configure_region(region: str) -> None:
    global _DEPLOYMENT_REGION
    _DEPLOYMENT_REGION = normalize_region(region)


def normalize_region(region: str | None) -> str:
    return (region or "").strip().lower()


async def _region_ok(session: AsyncSession, org_id: str) -> bool:
    if not _DEPLOYMENT_REGION:
        return True
    pinned = normalize_region(await session.scalar(select(Organization.data_region).where(Organization.id == org_id)))
    if not pinned or pinned == _DEPLOYMENT_REGION:
        return True
    logger.warning("refused a key for org %s: it is pinned to region %r and this deployment serves %r",
                   org_id, pinned, _DEPLOYMENT_REGION)
    return False


async def verify_api_key(session: AsyncSession, raw_key: str) -> AuthenticatedKey | None:
    if not raw_key or not raw_key.startswith(_KEY_PREFIX):
        return None
    now = datetime.now(timezone.utc)

    key = await _verify_by_hmac(session, raw_key, now)
    if key is None:
        key = await _verify_by_legacy_scan(session, raw_key, now)
    if key is not None and not await _region_ok(session, key.org_id):
        return None
    if key is not None:
        _remember(raw_key, key)
    return key


async def _verify_by_hmac(session: AsyncSession, raw_key: str, now: datetime) -> AuthenticatedKey | None:
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
    if not hmac.compare_digest(row.key_hmac, _key_hmac(raw_key)):
        return None
    if row.revoked_at is not None:
        return None
    if row.expires_at is not None and row.expires_at <= now:
        return None
    if row.last_used_at is None or (now - row.last_used_at) >= _LAST_USED_AT_UPDATE_INTERVAL:
        await session.execute(update(ApiKey).where(ApiKey.id == row.id).values(last_used_at=now))
    return AuthenticatedKey(
        org_id=row.org_id, key_prefix=row.key_prefix, scopes=_scopes_of(row.scopes),
        expires_at=row.expires_at,
    )


async def _verify_by_legacy_scan(session: AsyncSession, raw_key: str, now: datetime) -> AuthenticatedKey | None:
    if not _has_argon2 or PasswordHasher is None or _hasher is None:
        logger.warning("argon2-cffi is not installed; cannot verify legacy argon2 hash")
        return None

    prefix = raw_key[:_PREFIX_LEN]
    candidates = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_prefix == prefix, ApiKey.revoked_at.is_(None))
        )
    ).scalars().all()
    if not candidates:
        try:
            await asyncio.to_thread(_hasher.verify, _DUMMY_HASH, raw_key)
        except VerifyMismatchError:
            pass
        return None
    for candidate in candidates:
        try:
            await asyncio.to_thread(_hasher.verify, candidate.key_hash, raw_key)
        except VerifyMismatchError:
            continue
        except InvalidHashError:
            continue
        revoked_at = await session.scalar(select(ApiKey.revoked_at).where(ApiKey.id == candidate.id))
        if revoked_at is not None:
            return None
        if candidate.expires_at is not None and candidate.expires_at <= now:
            return None
        if candidate.last_used_at is None or (now - candidate.last_used_at) >= _LAST_USED_AT_UPDATE_INTERVAL:
            candidate.last_used_at = now
        current_hmac = _key_hmac(raw_key)
        if candidate.key_hmac != current_hmac:
            candidate.key_hmac = current_hmac
        return AuthenticatedKey(
            org_id=candidate.org_id, key_prefix=candidate.key_prefix,
            scopes=_scopes_of(candidate.scopes), expires_at=candidate.expires_at,
        )
    return None


current_org_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_org_id", default=None)

current_actor: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_actor", default=None)

current_scopes: contextvars.ContextVar[tuple[str, ...] | None] = contextvars.ContextVar(
    "current_scopes", default=None
)


class ScopeDenied(PermissionError):
    """The key authenticated, and is not allowed to do this."""

    def __init__(self, message: str, required: str, granted: tuple[str, ...] | None):
        super().__init__(message)
        self.required = required
        self.granted = granted


def require_scope(required: str) -> None:
    """Raise `ScopeDenied` unless the key in context holds `required`."""
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


current_user: contextvars.ContextVar[AuthenticatedUser | None] = contextvars.ContextVar(
    "current_user", default=None
)


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    org_id: str
    role: str
    email: str


async def verify_user_token(
    session: AsyncSession, raw_token: str, provider, *, jwks_cache=None,
) -> AuthenticatedUser | None:
    """Verify `raw_token` as an OIDC bearer JWT and resolve it to a `User`."""
    from hub import sso as sso_module

    try:
        claims = await asyncio.to_thread(
            sso_module.verify_bearer_token, raw_token, provider,
            jwks_cache=jwks_cache,
        )
    except sso_module.IdentityError:
        return None

    row = (
        await session.execute(
            select(User).where(
                User.issuer == claims.issuer,
                User.external_subject == claims.subject,
            )
        )
    ).scalar_one_or_none()
    if row is None or row.disabled_at is not None:
        return None
    if not await _region_ok(session, row.org_id):
        return None

    row.last_login_at = datetime.now(timezone.utc)
    return AuthenticatedUser(id=row.id, org_id=row.org_id, role=row.role, email=row.email)


class CapabilityDenied(PermissionError):
    def __init__(self, message: str, required: str, role: str):
        super().__init__(message)
        self.required = required
        self.role = role


def require_capability(tool_name: str) -> None:
    from hub import rbac as rbac_module

    person = current_user.get()
    if person is None:
        return
    required = rbac_module.capability_for_tool(tool_name)
    if not rbac_module.has_capability(person.role, required):
        raise CapabilityDenied(
            f"the signed-in user does not hold the {required!r} capability "
            f"(role: {person.role!r}). Ask an Owner to change this user's role "
            "with `python -m hub.manage set-user-role`.",
            required=required,
            role=person.role,
        )


def get_current_actor() -> str:
    from hub.audit import actor_for_api_key

    raw = current_actor.get()
    if not raw:
        return "unknown"
    if raw.startswith("user:"):
        return raw
    return actor_for_api_key(raw)


class PersonRequiredError(PermissionError):
    ...


def get_current_user() -> AuthenticatedUser:
    person = current_user.get()
    if person is None:
        raise PersonRequiredError(
            "this action requires a signed-in person (an OIDC bearer token "
            "linked via `hub.manage link-sso`), not just an org's API key."
        )
    return person
