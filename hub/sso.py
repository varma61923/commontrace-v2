"""Verifying a bearer JWT came from a trusted OIDC issuer.

WHY THIS EXISTS
---------------
hub/auth.py's own module docstring names OAuth/JWT as "an explicit
follow-up, not implemented here." This is that follow-up, scoped narrowly:
it verifies a token's signature, issuer, audience and lifetime against an
`IdentityProvider` this deployment configured, and returns the claims. It
does not run a login flow, does not talk to a token endpoint, does not
handle a refresh token, and does not decide who is allowed in -- that last
part is `hub/auth.py:verify_user_token`, which looks up the `hub/models.py`
`User` row the verified subject names. Nothing here auto-creates one.

WHAT "SSO ENFORCEMENT" MEANS HERE, PRECISELY
---------------------------------------------
An org can require that its members authenticate through its own identity
provider by never issuing them an API key and only ever linking their
`User` row to an OIDC subject (`hub.manage link-sso`). Once linked, that
person's access lives and dies with their standing at the IdP AND with
`disabled_at` here (hub/auth.py checks both, on every call). What this
module does NOT provide: a hosted login page, SAML, or SCIM-driven
automatic account creation when someone new appears at the IdP. Those are
still open -- see hub/README.md "Auth follow-ups".

THE THREE MISTAKES THAT MAKE JWT VERIFICATION A VULNERABILITY INSTEAD OF A
CONTROL, AND HOW EACH IS CLOSED
---------------------------------------------------------------------------
1. **Algorithm confusion.** A JWT header names its own algorithm, and a
   verifier that trusts that name will happily "verify" an attacker-forged
   HS256 token using an RSA PUBLIC key as the HMAC secret (public keys are,
   by definition, public) if the verifier ever passes the public key into
   an HMAC check. `ALLOWED_ALGORITHMS` is asymmetric-only (RS256/ES256) and
   is passed to `jwt.decode` as the allowlist, never derived from the
   token; PyJWT itself refuses to fall back to a different algorithm than
   the caller named.
2. **The "none" algorithm.** A spec-legal JWT can declare `alg: none` and
   carry no signature at all. It is not in `ALLOWED_ALGORITHMS`, so
   `jwt.decode` refuses it outright -- verified by a dedicated test.
3. **Trusting the token's own `kid` to fetch an arbitrary key.** This
   implementation only ever looks a `kid` up in the JWKS THIS deployment
   configured for THIS issuer; it never fetches a key from a URL the token
   itself supplies (a JWT header is attacker-controlled input, and treating
   any part of it as a fetch target is the SSRF version of mistake #1).

JWKS IS FETCHED ON A TTL, NOT PER REQUEST
------------------------------------------
`JWKSCache` re-fetches only after `ttl_seconds` has elapsed since the last
fetch, using an injected `fetch` callable -- so tests never touch the
network and a production deployment can point it at any HTTP client. A
static JWKS document (no `jwks_uri`) skips fetching entirely, for an
air-gapped deployment or a test fixture.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import jwt

# Asymmetric only. Never HS256/HS384/HS512: those are symmetric, meaning
# verification uses the SAME secret as signing, and the "secret" a resource
# server would need for them is exactly the thing an attacker supplies if a
# verifier is ever tricked into treating a public key as an HMAC key. There
# is no legitimate reason for this Hub -- which never issues tokens, only
# verifies ones an IdP issued -- to accept a symmetric algorithm.
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512")

#: How long a cached JWKS document is trusted before `JWKSCache` refetches
#: it. Long enough that a normal request volume never fetches on the hot
#: path; short enough that a rotated signing key is picked up within one
#: deploy's worth of traffic without an operator restart.
DEFAULT_JWKS_TTL_SECONDS = 3600

#: How much clock skew between this server and the IdP is tolerated on
#: `exp`/`iat`/`nbf`. Zero would make ordinary NTP drift an outage.
DEFAULT_CLOCK_SKEW_SECONDS = 60


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
    """One trusted OIDC issuer this deployment accepts tokens from.

    `jwks` is a static JWKS document (as OIDC discovery would return under
    the issuer's `jwks_uri`) when the deployment is configured offline;
    `jwks_uri` is fetched instead, on the TTL `JWKSCache` enforces. Passing
    both is redundant but not an error -- the static document wins, since it
    was explicitly supplied.
    """

    issuer: str
    audience: str
    jwks: dict | None = None
    jwks_uri: str = ""


class JWKSCache:
    """A JWKS document, refetched only after `ttl_seconds` has elapsed.

    `fetch` is injected rather than hardcoded to a specific HTTP client --
    tests pass a function that returns a canned document and never touch a
    socket; a real deployment passes one that calls its own configured
    client. `clock` is injected for the same reason survival.py's and
    decay.py's time-dependent code takes one: a test that has to sleep
    real seconds to exercise a TTL is a slow, flaky test.
    """

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

    def get(self, jwks_uri: str) -> dict:
        now = self._clock()
        cached = self._cached.get(jwks_uri)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        document = self._fetch(jwks_uri)
        self._cached[jwks_uri] = (now, document)
        return document

    def invalidate(self, jwks_uri: str = "") -> None:
        """Force the next `get` to refetch. `""` clears every entry."""
        if jwks_uri:
            self._cached.pop(jwks_uri, None)
        else:
            self._cached.clear()


def _key_for(jwks_document: dict, kid: str | None):
    """The public key matching `kid` in a JWKS document, or raise.

    Never falls back to "the only key" when `kid` is absent and the
    document holds exactly one: a token that omits `kid` is unusual enough,
    and key-confusion consequential enough, that guessing is the wrong
    default -- an IdP with more than one active key (the normal case during
    rotation) makes that guess wrong silently rather than loudly.
    """
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
            try:
                return jwt.algorithms.get_default_algorithms()[entry.get("alg", "RS256")].from_jwk(entry)
            except Exception as exc:  # noqa: BLE001 - a malformed JWK is a verification failure
                raise IdentityError(f"could not parse JWK for kid={kid!r}: {exc}") from None
    raise IdentityError(f"no key in this issuer's JWKS matches kid={kid!r}")


def verify_bearer_token(
    token: str,
    provider: IdentityProvider,
    *,
    jwks_cache: JWKSCache | None = None,
    now: float | None = None,
    clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS,
) -> IdentityClaims:
    """Verify `token` was issued by `provider` and return its claims.

    Raises `IdentityError` on anything short of a fully verified, current,
    correctly-audienced token -- there is no partial-credit return value,
    because a caller checking "was this verified" on a claims object it
    already has in hand is exactly the bug class (trusting unverified
    claims) this function exists to make impossible to reach by accident.
    """
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

    key = _key_for(jwks_document, header.get("kid"))

    try:
        payload = jwt.decode(
            token,
            key=key,
            algorithms=[alg],  # exactly the one the header named AND we allowlisted
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
    """Whether `token` has a JWT's shape: three dot-separated segments,
    none empty. Used to decide which verification path to try -- a raw API
    key (hub/auth.py's `ct_live_...` shape) never matches this, so the two
    credential kinds are distinguished by SHAPE, not by a fallible attempt
    to parse one and catch the exception.
    """
    parts = token.split(".")
    return len(parts) == 3 and all(parts)
