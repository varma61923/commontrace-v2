"""Verifying a bearer JWT came from a trusted OIDC issuer."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

import jwt

ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512")

DEFAULT_JWKS_TTL_SECONDS = 3600

DEFAULT_CLOCK_SKEW_SECONDS = 60

UNKNOWN_KID_REFETCH_SECONDS = 60

_KTY_FOR_ALG_PREFIX = {"RS": "RSA", "ES": "EC"}

logger = logging.getLogger("commontrace.hub.sso")


class IdentityError(Exception):
    """A bearer token could not be verified as issued by a trusted IdP."""


@dataclass(frozen=True)
class IdentityClaims:
    """What a verified token says about who is calling."""

    issuer: str
    subject: str
    email: str
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class IdentityProvider:
    """One trusted OIDC issuer this deployment accepts tokens from."""

    issuer: str
    audience: str
    jwks: dict | None = None
    jwks_uri: str = ""


def protected_resource_metadata(provider: IdentityProvider) -> dict:
    """RFC 9728 metadata consumed by remote MCP clients during OAuth sign-in.

    CommonTrace is the resource server; the customer's configured OIDC issuer
    remains the authorization server and owns the interactive login and token
    issuance flow.
    """
    return {
        "resource": provider.audience,
        "authorization_servers": [provider.issuer],
        "bearer_methods_supported": ["header"],
    }


def protected_resource_metadata_paths(provider: IdentityProvider) -> tuple[str, ...]:
    """RFC 9728 well-known paths for a resource whose audience has a path."""
    path = urlsplit(provider.audience).path.rstrip("/")
    bare = "/.well-known/oauth-protected-resource"
    return (bare, bare + path) if path else (bare,)


def protected_resource_metadata_url(provider: IdentityProvider) -> str | None:
    """Absolute metadata URL suitable for a WWW-Authenticate challenge."""
    parts = urlsplit(provider.audience)
    if parts.scheme not in ("https", "http") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}{protected_resource_metadata_paths(provider)[-1]}"


class JWKSCache:
    """A JWKS document, refetched only after `ttl_seconds` has elapsed."""

    def __init__(
        self,
        fetch: Callable[[str], dict],
        *,
        ttl_seconds: int = DEFAULT_JWKS_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._fetch = fetch
        self._ttl = max(1, ttl_seconds)
        self._clock = clock
        self._cached: dict[str, tuple[float, dict]] = {}
        self._retry_not_before: dict[str, float] = {}

    def get(self, jwks_uri: str) -> dict:
        now = self._clock()
        cached = self._cached.get(jwks_uri)
        if cached is not None and (
            now - cached[0] < self._ttl or now < self._retry_not_before.get(jwks_uri, now)
        ):
            return cached[1]
        return self._refetch(jwks_uri, now, cached)

    def refresh_for_unknown_kid(self, jwks_uri: str) -> dict | None:
        now = self._clock()
        cached = self._cached.get(jwks_uri)
        if cached is not None and (
            now - cached[0] < UNKNOWN_KID_REFETCH_SECONDS
            or now < self._retry_not_before.get(jwks_uri, now)
        ):
            return None
        return self._refetch(jwks_uri, now, cached)

    def _refetch(self, jwks_uri: str, now: float, cached: tuple[float, dict] | None) -> dict:
        """Fetch a fresh document; on failure keep serving the last good one.

        An IdP outage or a network blip must not turn every signed-in request
        into an unhandled 500, nor throw away keys that still verify. With a
        previous document the stale copy is served and the next attempt is
        deferred by UNKNOWN_KID_REFETCH_SECONDS, so a down IdP does not cost a
        blocking fetch per request. With none, it is an IdentityError -- a 401
        the caller can retry -- never an exception escaping the middleware.
        """
        try:
            document = self._fetch(jwks_uri)
            if not isinstance(document, dict):
                raise ValueError(f"JWKS document is a {type(document).__name__}, not an object")
        except Exception as exc:  # noqa: BLE001 - every fetch failure is handled the same way
            if cached is not None:
                logger.warning("JWKS refresh from %s failed (%s); serving the cached key set",
                               jwks_uri, type(exc).__name__)
                self._retry_not_before[jwks_uri] = now + UNKNOWN_KID_REFETCH_SECONDS
                return cached[1]
            raise IdentityError(
                f"could not fetch the identity provider's signing keys ({type(exc).__name__})"
            ) from None
        self._cached[jwks_uri] = (now, document)
        self._retry_not_before.pop(jwks_uri, None)
        return document

    def invalidate(self, jwks_uri: str = "") -> None:
        """Force the next `get` to refetch. `""` clears every entry."""
        if jwks_uri:
            self._cached.pop(jwks_uri, None)
            self._retry_not_before.pop(jwks_uri, None)
        else:
            self._cached.clear()
            self._retry_not_before.clear()


class UnknownKid(IdentityError):
    """No key in the JWKS document carries the token's `kid`."""


def _key_for(jwks_document: dict, kid: str | None, alg: str = "RS256"):
    keys = jwks_document.get("keys") if isinstance(jwks_document, dict) else None
    if not isinstance(keys, list):
        raise IdentityError("JWKS document has no usable 'keys' list")
    if kid is None:
        raise IdentityError(
            "token header carries no 'kid'; this verifier requires one to "
            "select the matching key rather than guessing"
        )
    for entry in keys:
        if isinstance(entry, dict) and entry.get("kid") == kid:
            expected_kty = _KTY_FOR_ALG_PREFIX.get(alg[:2])
            if entry.get("kty") != expected_kty:
                raise IdentityError(
                    f"JWK kid={kid!r} has kty={entry.get('kty')!r}, but the token is signed "
                    f"with {alg}, which needs kty={expected_kty!r}"
                )
            if "alg" in entry and entry["alg"] != alg:
                raise IdentityError(
                    f"JWK kid={kid!r} is published for {entry['alg']!r}, not the token's {alg!r}"
                )
            if entry.get("use", "sig") != "sig":
                raise IdentityError(f"JWK kid={kid!r} is not a signing key (use={entry.get('use')!r})")
            try:
                return jwt.algorithms.get_default_algorithms()[alg].from_jwk(entry)
            except Exception as exc:  # noqa: BLE001 - a malformed JWK is a verification failure
                raise IdentityError(f"could not parse JWK for kid={kid!r}: {exc}") from None
    raise UnknownKid(f"no key in this issuer's JWKS matches kid={kid!r}")


def verify_bearer_token(
    token: str,
    provider: IdentityProvider,
    *,
    jwks_cache: JWKSCache | None = None,
    now: float | None = None,
    clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS,
) -> IdentityClaims:
    """Verify `token` was issued by `provider` and return its claims."""
    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:  # noqa: BLE001 - any parse failure is an identity failure
        raise IdentityError(f"malformed token: {exc}") from None

    alg = header.get("alg")
    if alg not in ALLOWED_ALGORITHMS:
        raise IdentityError(
            f"algorithm {alg!r} is not accepted (allowed: {', '.join(ALLOWED_ALGORITHMS)}); "
            "this most often means either a misconfigured IdP or a forged token"
        )

    jwks_document = provider.jwks
    if jwks_document is None:
        if not provider.jwks_uri:
            raise IdentityError(
                f"identity provider {provider.issuer!r} has neither a static JWKS "
                "nor a jwks_uri configured"
            )
        if jwks_cache is None:
            raise IdentityError(
                "a jwks_uri provider requires a JWKSCache to fetch through -- "
                "verifying without one would mean fetching on every request"
            )
        jwks_document = jwks_cache.get(provider.jwks_uri)

    try:
        key = _key_for(jwks_document, header.get("kid"), alg)
    except UnknownKid:
        if provider.jwks is not None or jwks_cache is None:
            raise
        refreshed = jwks_cache.refresh_for_unknown_kid(provider.jwks_uri)
        if refreshed is None:
            raise
        key = _key_for(refreshed, header.get("kid"), alg)

    try:
        payload = jwt.decode(
            token,
            key=key,
            algorithms=[alg],
            audience=provider.audience,
            issuer=provider.issuer,
            leeway=clock_skew_seconds,
            options={
                "require": ["exp", "iat", "sub"],
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise IdentityError("token has expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise IdentityError(
            f"token audience does not match this deployment's configured audience "
            f"{provider.audience!r}"
        ) from exc
    except jwt.InvalidIssuerError as exc:
        raise IdentityError(
            f"token issuer does not match the configured provider {provider.issuer!r}"
        ) from exc
    except jwt.PyJWTError as exc:
        raise IdentityError(f"signature or claim verification failed: {exc}") from exc

    subject = str(payload.get("sub", ""))
    if not subject:
        raise IdentityError("token carries no 'sub' claim")

    return IdentityClaims(
        issuer=str(payload.get("iss", provider.issuer)),
        subject=subject,
        email=str(payload.get("email", "")),
        raw=dict(payload),
    )


def looks_like_jwt(token: str) -> bool:
    parts = token.split(".")
    return len(parts) == 3 and all(parts)
