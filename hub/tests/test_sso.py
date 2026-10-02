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
        payload = {"iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300}
        token = jwt.encode(payload, key, algorithm="RS256", headers={"kid": "k1"})
        with pytest.raises(sso.IdentityError, match='"sub"'):
            sso.verify_bearer_token(token, provider)


class TestAlgorithmConfusion:
    def _forge_hs256_with_public_key(self, pub, kid: str) -> str:
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
        other_key, _ = _keypair()
        token = _token(other_key, "k1")
        with pytest.raises(sso.IdentityError):
            sso.verify_bearer_token(token, provider)

    def test_clock_skew_within_tolerance_is_accepted(self, signing_key, provider):
        key, _ = signing_key
        now = int(time.time())
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
        key, _ = signing_key
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "iat": now, "exp": now + 300}
        token = jwt.encode(payload, key, algorithm="RS256")
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
        cache.get("uri")
        cache.get("uri")
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


class TestKeyRotation:
    def _setup(self, clock):
        old_key, old_pub = _keypair()
        new_key, new_pub = _keypair()
        published = {"keys": [_jwk(old_pub, "old")]}
        fetches = []

        def fetch(uri):
            fetches.append(uri)
            return json.loads(json.dumps(published))

        cache = sso.JWKSCache(fetch, clock=clock)
        provider = sso.IdentityProvider(
            issuer=ISSUER, audience=AUDIENCE, jwks_uri="https://idp/jwks.json"
        )
        return old_key, new_key, new_pub, published, fetches, cache, provider

    def test_a_token_signed_with_a_freshly_rotated_key_verifies(self):
        now = [0.0]
        old_key, new_key, new_pub, published, fetches, cache, provider = self._setup(lambda: now[0])
        assert sso.verify_bearer_token(_token(old_key, "old"), provider, jwks_cache=cache)
        published["keys"].append(_jwk(new_pub, "new"))
        now[0] = sso.UNKNOWN_KID_REFETCH_SECONDS + 1
        claims = sso.verify_bearer_token(_token(new_key, "new"), provider, jwks_cache=cache)
        assert claims.subject == "user-1"
        assert len(fetches) == 2

    def test_unknown_kids_refetch_at_most_once_per_interval(self):
        now = [0.0]
        old_key, new_key, _pub, _published, fetches, cache, provider = self._setup(lambda: now[0])
        sso.verify_bearer_token(_token(old_key, "old"), provider, jwks_cache=cache)
        for i in range(50):
            with pytest.raises(sso.IdentityError):
                sso.verify_bearer_token(_token(new_key, f"bogus-{i}"), provider, jwks_cache=cache)
        assert len(fetches) == 1
        now[0] = sso.UNKNOWN_KID_REFETCH_SECONDS + 1
        for i in range(50):
            with pytest.raises(sso.IdentityError):
                sso.verify_bearer_token(_token(new_key, f"bogus2-{i}"), provider, jwks_cache=cache)
        assert len(fetches) == 2

    def test_a_static_jwks_is_never_refetched(self, signing_key, provider):
        key, _pub = signing_key
        with pytest.raises(sso.IdentityError, match="matches kid"):
            sso.verify_bearer_token(_token(key, "nope"), provider)


class TestTheJwkMustFitTheTokensAlgorithm:
    def test_a_jwk_declaring_a_different_alg_is_refused(self, signing_key):
        key, pub = signing_key
        provider = _provider({"keys": [_jwk(pub, "k1", alg="RS512")]})
        with pytest.raises(sso.IdentityError, match="published for"):
            sso.verify_bearer_token(_token(key, "k1", alg="RS256"), provider)

    def test_an_encryption_key_is_not_a_signing_key(self, signing_key):
        key, pub = signing_key
        jwk = _jwk(pub, "k1")
        jwk["use"] = "enc"
        with pytest.raises(sso.IdentityError, match="not a signing key"):
            sso.verify_bearer_token(_token(key, "k1"), _provider({"keys": [jwk]}))

    def test_an_rsa_token_against_a_non_rsa_jwk_is_refused(self, signing_key):
        key, _pub = signing_key
        oct_jwk = {"kid": "k1", "kty": "oct", "k": "c2VjcmV0", "use": "sig"}
        with pytest.raises(sso.IdentityError, match="kty"):
            sso.verify_bearer_token(_token(key, "k1"), _provider({"keys": [oct_jwk]}))

    def test_a_jwk_without_alg_still_verifies_with_the_tokens_alg(self, signing_key):
        key, pub = signing_key
        jwk = _jwk(pub, "k1")
        del jwk["alg"]
        claims = sso.verify_bearer_token(_token(key, "k1", alg="RS384"), _provider({"keys": [jwk]}))
        assert claims.subject == "user-1"
