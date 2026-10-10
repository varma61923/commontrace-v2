"""OAuth 2.1 resource server: JWT access-token verification, metadata, gateway and MCP boundaries."""
from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature  # noqa: E402

from commontrace import gateway, holdout_io, oauth  # noqa: E402
from commontrace.exceptions import ConfigurationError  # noqa: E402

ISSUER = "https://login.example.com/"
AUDIENCE = "https://memory.example.com/mcp"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64int(value: int) -> str:
    return b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


class Keys:
    def __init__(self, prefix=""):
        self.prefix = prefix
        self.rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ec = ec.generate_private_key(ec.SECP256R1())
        self.ed = ed25519.Ed25519PrivateKey.generate()

    def jwks(self) -> dict:
        rn = self.rsa.public_key().public_numbers()
        en = self.ec.public_key().public_numbers()
        raw = self.ed.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {"keys": [
            {"kty": "RSA", "kid": self.prefix + "rsa-1", "use": "sig", "n": b64int(rn.n), "e": b64int(rn.e)},
            {"kty": "EC", "kid": self.prefix + "ec-1", "crv": "P-256", "x": b64int(en.x).rjust(43, "A"), "y": b64int(en.y).rjust(43, "A")},
            {"kty": "OKP", "kid": self.prefix + "ed-1", "crv": "Ed25519", "x": b64(raw)},
        ]}

    def sign(self, claims: dict, *, alg="RS256", kid=None, header=None) -> str:
        kid = kid or self.prefix + {"RS256": "rsa-1", "PS256": "rsa-1", "ES256": "ec-1", "EdDSA": "ed-1"}.get(alg, "")
        head = {"alg": alg, "typ": "at+jwt", **({"kid": kid} if kid else {}), **(header or {})}
        signing_input = (b64(json.dumps(head).encode()) + "." + b64(json.dumps(claims).encode())).encode()
        if alg == "RS256":
            sig = self.rsa.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        elif alg == "PS256":
            sig = self.rsa.sign(signing_input, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                          salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        elif alg == "ES256":
            r, s = decode_dss_signature(self.ec.sign(signing_input, ec.ECDSA(hashes.SHA256())))
            sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        elif alg == "EdDSA":
            sig = self.ed.sign(signing_input)
        else:
            sig = b"x"
        return signing_input.decode() + "." + b64(sig)


@pytest.fixture(scope="module")
def keys():
    return Keys()


def claims(**overrides):
    now = int(time.time())
    base = {"iss": ISSUER, "aud": AUDIENCE, "sub": "user@example.com", "exp": now + 600, "iat": now,
            "scope": "commontrace:admin"}
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


@pytest.fixture
def server(keys, tmp_path):
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps(keys.jwks()))
    return oauth.ResourceServer(oauth.OAuthConfig(issuer=ISSUER, audience=AUDIENCE, jwks_file=str(jwks)))


@pytest.mark.parametrize("alg", ["RS256", "PS256", "ES256", "EdDSA"])
def test_each_supported_algorithm_verifies(server, keys, alg):
    got = server.verify(keys.sign(claims(), alg=alg))
    assert got.subject == "user@example.com" and got.is_admin


@pytest.mark.parametrize("mutate,reason", [
    (lambda k: k.sign(claims(iss="https://evil.example.com/")), "issuer"),
    (lambda k: k.sign(claims(aud="https://other-api.example.com")), "audience"),
    (lambda k: k.sign(claims(exp=int(time.time()) - 3600)), "expired"),
    (lambda k: k.sign(claims(exp=None)), "expired"),
    (lambda k: k.sign(claims(nbf=int(time.time()) + 3600)), "nbf"),
    (lambda k: k.sign(claims(sub=None)), "subject"),
    (lambda k: k.sign(claims(), alg="HS256", kid="rsa-1"), "algorithm"),
    (lambda k: k.sign(claims(), alg="none", kid="rsa-1"), "algorithm"),
    (lambda k: k.sign(claims(), kid="unknown"), "no signing key"),
    (lambda k: k.sign(claims(), header={"typ": "id+jwt"}), "type"),
    (lambda k: k.sign(claims(), header={"crit": ["exp"]}), "critical"),
    (lambda k: k.sign(claims())[:-6] + "AAAAAA", "signature"),
    (lambda k: "not.a.jwt!", "base64url"),
])
def test_bad_tokens_are_refused(server, keys, mutate, reason):
    with pytest.raises(oauth.InvalidToken, match=reason):
        server.verify(mutate(keys))


def test_signature_from_another_key_is_refused(server):
    other = Keys()
    with pytest.raises(oauth.InvalidToken, match="signature"):
        server.verify(other.sign(claims()))


def test_audience_list_and_scp_claim_are_understood(server, keys):
    got = server.verify(keys.sign(claims(aud=["x", AUDIENCE], scope=None, scp=["commontrace:memory"])))
    assert got.scopes == {"commontrace:memory"} and not got.is_admin
    with pytest.raises(oauth.InvalidToken, match="scope"):
        server.verify(keys.sign(claims(scope="read")), required_scopes=("commontrace:mcp",))


def test_principal_ids_are_stable_registry_compatible_and_issuer_bound(server, keys):
    a = server.verify(keys.sign(claims(sub="Alice Smith/admin"))).principal()
    b = server.verify(keys.sign(claims(sub="Alice Smith/admin"))).principal()
    assert a == b and a["id"].startswith("oauth-Alice_Smith_admin-") and a["scopes"] == ["agent:" + a["id"]]
    assert len(a["id"]) <= 64


def test_rotated_keys_are_fetched_on_an_unknown_kid(keys, monkeypatch):
    fetched = []
    first = keys.jwks()
    rotated = Keys(prefix="2026-")

    def fetch(url):
        fetched.append(url)
        return json.dumps(first if len(fetched) == 1 else rotated.jwks()).encode()

    srv = oauth.ResourceServer(oauth.OAuthConfig(issuer=ISSUER, audience=AUDIENCE,
                                                 jwks_url="https://login.example.com/jwks"), fetch=fetch)
    assert srv.verify(keys.sign(claims())).is_admin
    monkeypatch.setattr(oauth, "JWKS_MIN_REFRESH_SECONDS", 0.0)
    assert srv.verify(rotated.sign(claims(), alg="EdDSA")).is_admin
    assert len(fetched) == 2


def test_configuration_is_validated(tmp_path):
    with pytest.raises(ConfigurationError):
        oauth.OAuthConfig(issuer=ISSUER, audience="")
    with pytest.raises(ConfigurationError):
        oauth.OAuthConfig(issuer=ISSUER, audience=AUDIENCE, jwks_url="http://login.example.com/jwks")
    with pytest.raises(ConfigurationError):
        oauth.OAuthConfig(issuer=ISSUER, audience=AUDIENCE, jwks_file="x", algorithms=("HS256",))
    assert oauth.load_config(str(tmp_path)) is None


def test_metadata_follows_rfc9728(server):
    meta = server.config.metadata()
    assert meta["resource"] == AUDIENCE and meta["authorization_servers"] == [ISSUER]
    assert oauth.metadata_paths(server.config) == (oauth.WELL_KNOWN, oauth.WELL_KNOWN + "/mcp")
    assert oauth.metadata_url(server.config) == "https://memory.example.com/.well-known/oauth-protected-resource/mcp"


# -- gateway ---------------------------------------------------------------------------------------

@pytest.fixture
def oauth_gateway(tmp_path, keys, monkeypatch):
    root = str(tmp_path / "store")
    holdout_io.configure(root, rate=0.5, salt="oauth-tests")
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps(keys.jwks()))
    monkeypatch.setenv("COMMONTRACE_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("COMMONTRACE_OAUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("COMMONTRACE_OAUTH_JWKS_FILE", str(jwks))
    return gateway.Gateway(root, token="t" * 40)


def call(g, method, path, token=None, body=None):
    headers = {"Host": "localhost:8787"}
    if token:
        headers["Authorization"] = "Bearer " + token
    response = g.handle(method, path, headers, json.dumps(body).encode() if body is not None else None)
    return response.status, json.loads(response.body), response.headers


def test_gateway_serves_metadata_and_challenges_with_it(oauth_gateway):
    status, meta, _ = call(oauth_gateway, "GET", "/.well-known/oauth-protected-resource/mcp")
    assert status == 200 and meta["authorization_servers"] == [ISSUER]
    status, _body, headers = call(oauth_gateway, "GET", "/v1/status")
    assert status == 401 and 'resource_metadata="https://memory.example.com/' in headers["WWW-Authenticate"]


def test_gateway_admin_token_acts_as_operator(oauth_gateway, keys):
    assert call(oauth_gateway, "GET", "/v1/status", keys.sign(claims()))[0] == 200


def test_gateway_memory_token_is_confined_to_its_own_scope(oauth_gateway, keys):
    token = keys.sign(claims(scope="commontrace:memory", sub="agent-7"))
    assert call(oauth_gateway, "GET", "/v1/status", token)[0] == 403
    status, added, _ = call(oauth_gateway, "POST", "/v1/memory/add", token,
                            {"text": "The release window is Friday", "context": ["agent:someone-else"]})
    assert status == 200, added
    owner = call(oauth_gateway, "POST", "/v1/memory/search", keys.sign(claims()),
                 {"query": "release window", "context": ["agent:someone-else"]})[1]
    assert not json.dumps(owner).count("release window is Friday")


def test_gateway_rejects_bad_and_unscoped_tokens(oauth_gateway, keys):
    status, _body, headers = call(oauth_gateway, "GET", "/v1/status", keys.sign(claims(exp=int(time.time()) - 999)))
    assert status == 401 and 'error="invalid_token"' in headers["WWW-Authenticate"]
    status, _body, headers = call(oauth_gateway, "GET", "/v1/status", keys.sign(claims(scope="profile")))
    assert status == 403 and 'error="insufficient_scope"' in headers["WWW-Authenticate"]
    assert call(oauth_gateway, "GET", "/v1/status", "t" * 40)[0] == 200  # the file token still works


def test_gateway_without_oauth_keeps_plain_challenge(tmp_path):
    root = str(tmp_path / "store")
    holdout_io.configure(root, rate=0.5, salt="oauth-tests")
    g = gateway.Gateway(root, token="t" * 40)
    status, _body, headers = call(g, "GET", "/v1/status")
    assert status == 401 and "WWW-Authenticate" not in headers
    assert call(g, "GET", "/.well-known/oauth-protected-resource")[0] == 404


# -- MCP HTTP boundary -----------------------------------------------------------------------------

def _asgi(boundary, path, token=None, method="POST"):
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    boundary.app = app
    headers = [(b"authorization", ("Bearer " + token).encode())] if token else []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    asyncio.run(boundary({"type": "http", "method": method, "path": path, "headers": headers}, receive, send))
    start = sent[0]
    return start["status"], dict(start["headers"]), b"".join(m.get("body", b"") for m in sent[1:])


def test_mcp_boundary_accepts_scoped_jwts_and_serves_metadata(server, keys):
    from commontrace.mcp_transport import BearerBoundary

    boundary = BearerBoundary(None, lambda: "f" * 40, oauth_server=server)
    assert _asgi(boundary, "/mcp", keys.sign(claims(scope="commontrace:mcp")))[0] == 200
    assert _asgi(boundary, "/mcp", "f" * 40)[0] == 200
    status, headers, _ = _asgi(boundary, "/mcp", keys.sign(claims(scope="commontrace:memory")))
    assert status == 401 and b"resource_metadata=" in headers[b"www-authenticate"]
    status, _headers, body = _asgi(boundary, "/.well-known/oauth-protected-resource/mcp", method="GET")
    assert status == 200 and json.loads(body)["resource"] == AUDIENCE
