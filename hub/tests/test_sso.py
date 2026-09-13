"""Verifying a bearer JWT came from a trusted OIDC issuer.

What these tests defend, in order of how badly getting it wrong would hurt:

1. **Algorithm confusion cannot forge an identity.** An attacker who knows a
   deployment's RSA PUBLIC key (which is, by definition, public) can try to
   sign an HS256 token using that public key's bytes as the HMAC secret. A
   verifier that ever passes the same key material into both an asymmetric
   and a symmetric check is forgeable by anyone. The forged token here is
   built by hand, byte for byte, rather than through `jwt.encode` -- PyJWT's
   own encoder refuses to build it, which would make the test pass for a
   reason that says nothing about the verifier.
2. **`alg: none` is refused.** A spec-legal, signature-free token must not
   authenticate anyone.
3. **Issuer, audience, and expiry are all actually checked** -- not merely
   present in the code, but changing any one of them independently is
   caught.
4. **No network on the hot path.** `JWKSCache` refetches only after its TTL,
   verified with an injected clock rather than a real sleep.
5. **`kid` selects a key; it is never guessed.** A JWKS with more than one
   active key (the normal case mid-rotation) must not silently pick the
   wrong one, and a token naming no `kid` at all must not fall back to "the
   only key" quietly.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from hub import sso

ISSUER = "https://idp.example.test/"
AUDIENCE = "commontrace-hub"


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def _jwk(pub, kid: str, alg: str = "RS256") -> dict:
    raw = json.loads(RSAAlgorithm.to_jwk(pub))
    raw["kid"] = kid
    raw["alg"] = alg
    raw["use"] = "sig"
    return raw


def _provider(jwks: dict, **overrides) -> sso.IdentityProvider:
    kwargs = dict(issuer=ISSUER, audience=AUDIENCE, jwks=jwks)
    kwargs.update(overrides)
    return sso.IdentityProvider(**kwargs)


def _token(key, kid: str, *, alg="RS256", now=None, **claims) -> str:
    now = now if now is not None else int(time.time())
    payload = {
        "iss": ISSUER, "aud": AUDIENCE, "sub": "user-1",
        "iat": now, "exp": now + 300,
    }
    payload.update(claims)
    return jwt.encode(payload, key, algorithm=alg, headers={"kid": kid})


@pytest.fixture
def signing_key():
    return _keypair()


@pytest.fixture
def provider(signing_key):
    _, pub = signing_key
    return _provider({"keys": [_jwk(pub, "k1")]})


class TestAValidToken:
    def test_verifies_and_returns_claims(self, signing_key, provider):
        key, _ = signing_key
        token = _token(key, "k1", email="person@example.test")
        claims = sso.verify_bearer_token(token, provider)
        assert claims.issuer == ISSUER
        assert claims.subject == "user-1"
        assert claims.email == "person@example.test"

    def test_carries_the_full_raw_payload_too(self, signing_key, provider):
        key, _ = signing_key
        token = _token(key, "k1", custom_claim="x")
        claims = sso.verify_bearer_token(token, provider)
        assert claims.raw["custom_claim"] == "x"

    def test_a_token_missing_sub_is_refused(self, signing_key, provider):
        key, _ = signing_key
        now = int(time.time())
        # Build without 'sub' -- jwt.encode requires nothing about claim
        # shape, so this constructs a technically-valid, subject-less JWT.
        payload = {"iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300}
        token = jwt.encode(payload, key, algorithm="RS256", headers={"kid": "k1"})
        with pytest.raises(sso.IdentityError, match='"sub"'):
            sso.verify_bearer_token(token, provider)


class TestAlgorithmConfusion:
    """The attack this exists to make impossible: an attacker holds the
    deployment's PUBLIC key (public by definition) and tries to use it as an
    HMAC secret for a forged HS256 token."""

    def _forge_hs256_with_public_key(self, pub, kid: str) -> str:
        """Built byte-for-byte, not via jwt.encode -- PyJWT's own encoder
        refuses to sign with a key that looks like a PEM-encoded asymmetric
        key, which would make this test pass without ever exercising the
        verifier's own defense."""
        def b64u(data: bytes) -> bytes:
            return base64.urlsafe_b64encode(data).rstrip(b"=")

        pub_pem = pub.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        now = int(time.time())
        header = b64u(json.dumps({"alg": "HS256", "kid": kid, "typ": "JWT"}).encode())
        payload = b64u(json.dumps({
            "iss": ISSUER, "aud": AUDIENCE, "sub": "attacker",
            "iat": now, "exp": now + 300,
        }).encode())
        signing_input = header + b"." + payload
        sig = b64u(hmac.new(pub_pem, signing_input, hashlib.sha256).digest())
        return (signing_input + b"." + sig).decode()

    def test_hs256_forged_with_the_public_key_is_rejected(self, signing_key, provider):
        _, pub = signing_key
        forged = self._forge_hs256_with_public_key(pub, "k1")
        with pytest.raises(sso.IdentityError, match="not accepted"):
            sso.verify_bearer_token(forged, provider)

    def test_none_algorithm_is_rejected(self, provider):
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "iat": now, "exp": now + 300}
        unsigned = jwt.encode(payload, key="", algorithm="none")
        with pytest.raises(sso.IdentityError, match="not accepted"):
            sso.verify_bearer_token(unsigned, provider)

    def test_every_allowed_algorithm_is_asymmetric(self):
        symmetric = {"HS256", "HS384", "HS512", "none"}
        assert not (set(sso.ALLOWED_ALGORITHMS) & symmetric)


class TestClaimVerification:
    def test_wrong_audience_is_rejected(self, signing_key, provider):
        key, _ = signing_key
        token = _token(key, "k1", aud="someone-else")
        with pytest.raises(sso.IdentityError, match="audience"):
            sso.verify_bearer_token(token, provider)

    def test_wrong_issuer_is_rejected(self, signing_key, provider):
        key, _ = signing_key
        token = _token(key, "k1", iss="https://not-the-real-idp.test/")
        with pytest.raises(sso.IdentityError, match="issuer"):
            sso.verify_bearer_token(token, provider)

    def test_an_expired_token_is_rejected(self, signing_key, provider):
        key, _ = signing_key
        now = int(time.time())
        token = _token(key, "k1", now=now - 1000, exp=now - 500)
        with pytest.raises(sso.IdentityError, match="expired"):
            sso.verify_bearer_token(token, provider)

    def test_a_token_from_a_different_key_is_rejected(self, provider):
        """Simulates a stolen 'kid' with the wrong signature behind it: a
        second, unrelated key claiming the same kid as the trusted one."""
        other_key, _ = _keypair()
        token = _token(other_key, "k1")  # kid matches, signature does not
        with pytest.raises(sso.IdentityError):
            sso.verify_bearer_token(token, provider)

    def test_clock_skew_within_tolerance_is_accepted(self, signing_key, provider):
        key, _ = signing_key
        now = int(time.time())
        # Issued 30s "in the future" from this server's clock -- ordinary
        # NTP drift, not an attack.
        token = _token(key, "k1", now=now + 30)
        sso.verify_bearer_token(token, provider, clock_skew_seconds=60)

    def test_clock_skew_beyond_tolerance_is_rejected(self, signing_key, provider):
        key, _ = signing_key
        now = int(time.time())
        token = _token(key, "k1", now=now + 3600)
        with pytest.raises(sso.IdentityError):
            sso.verify_bearer_token(token, provider, clock_skew_seconds=60)


class TestKeySelection:
    def test_a_jwks_with_multiple_keys_picks_the_matching_kid(self, signing_key):
        key, pub = signing_key
        other_key, other_pub = _keypair()
        jwks = {"keys": [_jwk(other_pub, "old-key"), _jwk(pub, "new-key")]}
        provider = _provider(jwks)
        token = _token(key, "new-key")
        claims = sso.verify_bearer_token(token, provider)
        assert claims.subject == "user-1"

    def test_a_token_with_no_kid_is_refused_rather_than_guessed(self, signing_key, provider):
        """Even with exactly one key in the JWKS, a missing kid must not
        silently resolve to it -- an IdP with more than one active key
        mid-rotation makes that guess wrong, silently."""
        key, _ = signing_key
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "iat": now, "exp": now + 300}
        token = jwt.encode(payload, key, algorithm="RS256")  # no kid header
        with pytest.raises(sso.IdentityError, match="kid"):
            sso.verify_bearer_token(token, provider)

    def test_an_unknown_kid_is_refused(self, signing_key, provider):
        key, _ = signing_key
        token = _token(key, "no-such-key")
        with pytest.raises(sso.IdentityError, match="no key"):
            sso.verify_bearer_token(token, provider)

    def test_an_empty_jwks_is_refused(self, signing_key):
        key, _ = signing_key
        provider = _provider({"keys": []})
        token = _token(key, "k1")
        with pytest.raises(sso.IdentityError, match="no key"):
            sso.verify_bearer_token(token, provider)

    def test_a_malformed_jwks_document_is_refused_not_a_crash(self, signing_key):
        key, _ = signing_key
        provider = _provider({"not": "a keys list"})
        token = _token(key, "k1")
        with pytest.raises(sso.IdentityError, match="keys"):
            sso.verify_bearer_token(token, provider)


class TestJWKSCache:
    def test_fetches_once_within_the_ttl(self):
        calls = []

        def fetch(uri):
            calls.append(uri)
            return {"keys": []}

        clock = iter([0.0, 10.0, 20.0]).__next__
        cache = sso.JWKSCache(fetch, ttl_seconds=3600, clock=clock)
        cache.get("https://idp/.well-known/jwks.json")
        cache.get("https://idp/.well-known/jwks.json")
        cache.get("https://idp/.well-known/jwks.json")
        assert len(calls) == 1

    def test_refetches_after_the_ttl_elapses(self):
        calls = []

        def fetch(uri):
            calls.append(uri)
            return {"keys": []}

        times = iter([0.0, 5.0, 4000.0])
        cache = sso.JWKSCache(fetch, ttl_seconds=3600, clock=lambda: next(times))
        cache.get("uri")
        cache.get("uri")  # within TTL, not refetched
        cache.get("uri")  # past TTL, refetched
        assert len(calls) == 2

    def test_different_uris_are_cached_independently(self):
        calls = []

        def fetch(uri):
            calls.append(uri)
            return {"keys": [], "uri": uri}

        cache = sso.JWKSCache(fetch, clock=lambda: 0.0)
        a = cache.get("uri-a")
        b = cache.get("uri-b")
        assert a["uri"] == "uri-a" and b["uri"] == "uri-b"
        assert len(calls) == 2

    def test_invalidate_forces_a_refetch(self):
        calls = []

        def fetch(uri):
            calls.append(1)
            return {"keys": []}

        cache = sso.JWKSCache(fetch, clock=lambda: 0.0)
        cache.get("uri")
        cache.invalidate("uri")
        cache.get("uri")
        assert len(calls) == 2

    def test_verify_bearer_token_fetches_through_the_cache(self, signing_key):
        key, pub = signing_key
        jwks = {"keys": [_jwk(pub, "k1")]}
        calls = []

        def fetch(uri):
            calls.append(uri)
            return jwks

        cache = sso.JWKSCache(fetch, clock=lambda: 0.0)
        provider = sso.IdentityProvider(
            issuer=ISSUER, audience=AUDIENCE, jwks_uri="https://idp/jwks.json"
        )
        token = _token(key, "k1")
        claims = sso.verify_bearer_token(token, provider, jwks_cache=cache)
        assert claims.subject == "user-1"
        assert calls == ["https://idp/jwks.json"]

    def test_a_jwks_uri_provider_without_a_cache_is_refused(self, signing_key):
        """Verifying without a cache would mean fetching on every single
        request -- refused loudly rather than silently doing that."""
        key, pub = signing_key
        provider = sso.IdentityProvider(
            issuer=ISSUER, audience=AUDIENCE, jwks_uri="https://idp/jwks.json"
        )
        token = _token(key, "k1")
        with pytest.raises(sso.IdentityError, match="JWKSCache"):
            sso.verify_bearer_token(token, provider)

    def test_a_provider_with_neither_static_jwks_nor_uri_is_refused(self, signing_key):
        key, _ = signing_key
        provider = sso.IdentityProvider(issuer=ISSUER, audience=AUDIENCE)
        token = _token(key, "k1")
        with pytest.raises(sso.IdentityError, match="neither"):
            sso.verify_bearer_token(token, provider)


class TestMalformedInput:
    def test_a_non_jwt_string_is_refused_not_a_crash(self, provider):
        with pytest.raises(sso.IdentityError, match="malformed"):
            sso.verify_bearer_token("not-a-jwt-at-all", provider)

    def test_an_empty_string_is_refused(self, provider):
        with pytest.raises(sso.IdentityError):
            sso.verify_bearer_token("", provider)


class TestLooksLikeJwt:
    def test_an_api_key_does_not_look_like_a_jwt(self):
        assert not sso.looks_like_jwt("ct_live_" + "a" * 43)

    def test_a_real_jwt_looks_like_one(self, signing_key):
        key, _ = signing_key
        token = _token(key, "k1")
        assert sso.looks_like_jwt(token)

    def test_a_string_with_empty_segments_does_not_count(self):
        assert not sso.looks_like_jwt("a..b")
        assert not sso.looks_like_jwt("..")
        assert not sso.looks_like_jwt("a.b")
        assert not sso.looks_like_jwt("")
