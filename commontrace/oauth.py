"""OAuth 2.1 resource server: accept JWT access tokens from your own authorization server.

CommonTrace never issues tokens or runs a login flow. An operator points it at an
authorization server they already run (Okta, Entra ID, Auth0, Keycloak, ...) and
CommonTrace verifies each bearer access token locally:

* signature against the issuer's JWKS (RS256, PS256, ES256 or EdDSA only --
  ``none`` and shared-secret HMAC algorithms are refused outright);
* ``iss`` equal to the configured issuer, ``aud`` containing this resource
  (RFC 8707 audience binding, so a token minted for another API is useless here);
* ``exp`` required, ``nbf``/``iat`` honoured, with a small clock leeway;
* scopes from ``scope`` (space separated) or ``scp`` (list).

Unauthenticated clients discover the authorization server from
``/.well-known/oauth-protected-resource`` (RFC 9728), which the MCP HTTP
transport and the gateway both serve when OAuth is configured; a 401 names that
document in ``WWW-Authenticate`` as the MCP authorization specification asks.

Configuration (environment, or the same keys in ``memory/oauth.json``)::

    COMMONTRACE_OAUTH_ISSUER     https://login.example.com/
    COMMONTRACE_OAUTH_AUDIENCE   https://memory.example.com/mcp
    COMMONTRACE_OAUTH_JWKS_URL   https://login.example.com/.well-known/jwks.json
    COMMONTRACE_OAUTH_JWKS_FILE  (instead of the URL, for air-gapped hosts)

Scopes: ``commontrace:admin`` acts as the store operator; ``commontrace:memory``
acts as an agent confined to its own ``agent:<id>`` scope, exactly like a key
from ``commontrace init --agent``. The verification primitives need the optional
``cryptography`` package (``pip install 'commontrace[security]'``).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field

from commontrace.exceptions import CapabilityError, ConfigurationError

SCOPE_ADMIN = "commontrace:admin"
SCOPE_MEMORY = "commontrace:memory"
SCOPE_MCP = "commontrace:mcp"  # whole-store MCP access, the same grant as the local file token
ALGORITHMS = ("RS256", "PS256", "ES256", "EdDSA")
MAX_TOKEN_CHARS = 8192
MAX_JWKS_BYTES = 256 * 1024
JWKS_TTL_SECONDS = 600.0
JWKS_MIN_REFRESH_SECONDS = 60.0
DEFAULT_LEEWAY = 60
_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")


class InvalidToken(Exception):
    """The bearer is not an acceptable access token. The message is safe to log, never to echo."""


@dataclass(frozen=True)
class OAuthConfig:
    issuer: str
    audience: str
    jwks_url: str = ""
    jwks_file: str = ""
    algorithms: tuple[str, ...] = ALGORITHMS
    leeway: int = DEFAULT_LEEWAY
    scopes_supported: tuple[str, ...] = (SCOPE_ADMIN, SCOPE_MEMORY, SCOPE_MCP)

    def __post_init__(self):
        if not self.issuer or not self.audience:
            raise ConfigurationError("OAuth needs both an issuer and an audience")
        if bool(self.jwks_url) == bool(self.jwks_file):
            raise ConfigurationError("set exactly one of the OAuth JWKS URL or JWKS file")
        if self.jwks_url and not (self.jwks_url.startswith("https://") or _loopback(self.jwks_url)):
            raise ConfigurationError("the JWKS URL must use https (loopback http is allowed for testing)")
        unknown = set(self.algorithms) - set(ALGORITHMS)
        if not self.algorithms or unknown:
            raise ConfigurationError(f"OAuth algorithms must be a subset of {', '.join(ALGORITHMS)}")
        if not 0 <= self.leeway <= 300:
            raise ConfigurationError("OAuth clock leeway must be between 0 and 300 seconds")

    def metadata(self) -> dict:
        """RFC 9728 protected-resource metadata."""
        return {"resource": self.audience, "authorization_servers": [self.issuer],
                "bearer_methods_supported": ["header"], "scopes_supported": list(self.scopes_supported),
                "resource_signing_alg_values_supported": list(self.algorithms)}


@dataclass(frozen=True)
class Claims:
    subject: str
    scopes: frozenset[str]
    issuer: str
    expires_at: int
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def is_admin(self) -> bool:
        return SCOPE_ADMIN in self.scopes

    def agent_id(self) -> str:
        """A stable, registry-compatible principal id for this subject."""
        cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", self.subject)[:32].strip("._-") or "subject"
        digest = hashlib.sha256((self.issuer + "\0" + self.subject).encode("utf-8")).hexdigest()[:10]
        return f"oauth-{cleaned}-{digest}"

    def principal(self) -> dict:
        agent = self.agent_id()
        return {"id": agent, "scopes": ["agent:" + agent], "oauth": True, "self_enrolled": False}


def _loopback(url: str) -> bool:
    from commontrace import offline

    return offline.is_loopback(url)


def load_config(root: str | None = None) -> OAuthConfig | None:
    """The configured resource server, or None when OAuth is not enabled."""
    values: dict = {}
    if root:
        from commontrace import paths

        path = os.path.join(paths.memory_dir(root), "oauth.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ConfigurationError("memory/oauth.json must be a JSON object")
            values.update(loaded)
    env = {"issuer": "COMMONTRACE_OAUTH_ISSUER", "audience": "COMMONTRACE_OAUTH_AUDIENCE",
           "jwks_url": "COMMONTRACE_OAUTH_JWKS_URL", "jwks_file": "COMMONTRACE_OAUTH_JWKS_FILE",
           "algorithms": "COMMONTRACE_OAUTH_ALGORITHMS", "leeway": "COMMONTRACE_OAUTH_LEEWAY"}
    for key, name in env.items():
        if os.environ.get(name):
            values[key] = os.environ[name]
    if not values.get("issuer"):
        return None
    algorithms = values.get("algorithms", ALGORITHMS)
    if isinstance(algorithms, str):
        algorithms = tuple(a.strip() for a in algorithms.split(",") if a.strip())
    try:
        leeway = int(values.get("leeway", DEFAULT_LEEWAY))
    except (TypeError, ValueError):
        raise ConfigurationError("OAuth leeway must be an integer number of seconds") from None
    return OAuthConfig(issuer=str(values["issuer"]), audience=str(values.get("audience", "")),
                       jwks_url=str(values.get("jwks_url", "")), jwks_file=str(values.get("jwks_file", "")),
                       algorithms=tuple(algorithms), leeway=leeway)


def _b64decode(segment: str) -> bytes:
    if not _SEGMENT.match(segment):
        raise InvalidToken("token segment is not base64url")
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _b64int(value: object) -> int:
    if not isinstance(value, str):
        raise InvalidToken("JWK integer field missing")
    return int.from_bytes(_b64decode(value), "big")


def looks_like_jwt(token: str) -> bool:
    return isinstance(token, str) and token.count(".") == 2 and len(token) <= MAX_TOKEN_CHARS


class JWKSCache:
    """The issuer's signing keys, refreshed on a TTL and on an unknown ``kid`` (rate limited)."""

    def __init__(self, config: OAuthConfig, *, fetch=None):
        self._config = config
        self._fetch = fetch or self._http_fetch
        self._keys: list[dict] = []
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def _http_fetch(self, url: str) -> bytes:
        from commontrace import offline

        offline.check_url(url, "the OAuth JWKS endpoint")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}),
                         timeout=10) as response:  # nosec B310 - https (or loopback) checked in OAuthConfig
            raw = response.read(MAX_JWKS_BYTES + 1)
        if len(raw) > MAX_JWKS_BYTES:
            raise InvalidToken("JWKS document is too large")
        return raw

    def _load(self) -> None:
        if self._config.jwks_file:
            with open(self._config.jwks_file, "rb") as fh:
                raw = fh.read(MAX_JWKS_BYTES + 1)
        else:
            raw = self._fetch(self._config.jwks_url)
        document = json.loads(raw)
        keys = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(keys, list):
            raise InvalidToken("JWKS document has no keys")
        self._keys = [k for k in keys if isinstance(k, dict) and k.get("use", "sig") == "sig"]
        self._loaded_at = time.monotonic()

    def key_for(self, kid: str | None, alg: str) -> dict:
        with self._lock:
            now = time.monotonic()
            if not self._keys or now - self._loaded_at > JWKS_TTL_SECONDS:
                self._load()
            match = self._select(kid, alg)
            if match is None and kid and now - self._loaded_at > JWKS_MIN_REFRESH_SECONDS:
                self._load()  # the issuer may have rotated keys since the last fetch
                match = self._select(kid, alg)
        if match is None:
            raise InvalidToken("no signing key matches the token")
        return match

    def _select(self, kid: str | None, alg: str) -> dict | None:
        kty = {"RS256": "RSA", "PS256": "RSA", "ES256": "EC", "EdDSA": "OKP"}[alg]
        candidates = [k for k in self._keys if k.get("kty") == kty and k.get("alg", alg) == alg]
        if kid is not None:
            candidates = [k for k in candidates if k.get("kid") == kid]
        return candidates[0] if len(candidates) == 1 else None


def _verify_signature(alg: str, jwk: dict, signing_input: bytes, signature: bytes) -> None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    except ImportError:
        raise CapabilityError("OAuth token verification needs the security extra",
                              remediation="pip install 'commontrace[security]'") from None
    try:
        if alg in ("RS256", "PS256"):
            key = rsa.RSAPublicNumbers(_b64int(jwk.get("e")), _b64int(jwk.get("n"))).public_key()
            if key.key_size < 2048:
                raise InvalidToken("RSA signing keys must be at least 2048 bits")
            pad = padding.PKCS1v15() if alg == "RS256" else padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)
            key.verify(signature, signing_input, pad, hashes.SHA256())
        elif alg == "ES256":
            if jwk.get("crv") != "P-256" or len(signature) != 64:
                raise InvalidToken("ES256 needs a P-256 key and a 64-byte signature")
            key = ec.EllipticCurvePublicNumbers(_b64int(jwk.get("x")), _b64int(jwk.get("y")),
                                                ec.SECP256R1()).public_key()
            der = encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
            key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
        elif alg == "EdDSA":
            if jwk.get("crv") != "Ed25519":
                raise InvalidToken("EdDSA tokens must use Ed25519")
            ed25519.Ed25519PublicKey.from_public_bytes(_b64decode(str(jwk.get("x", "")))).verify(
                signature, signing_input)
        else:  # pragma: no cover - guarded by the algorithm allowlist
            raise InvalidToken("unsupported algorithm")
    except InvalidSignature:
        raise InvalidToken("signature does not verify") from None
    except ValueError:
        raise InvalidToken("signing key is malformed") from None


class ResourceServer:
    """Verifies bearer access tokens for one configured issuer and audience."""

    def __init__(self, config: OAuthConfig, *, fetch=None, clock=time.time):
        self.config = config
        self._keys = JWKSCache(config, fetch=fetch)
        self._clock = clock

    def verify(self, token: str, *, required_scopes: tuple[str, ...] = ()) -> Claims:
        if not looks_like_jwt(token):
            raise InvalidToken("not a JWT access token")
        header_b64, payload_b64, signature_b64 = token.split(".")
        if not all(_SEGMENT.match(part) for part in (header_b64, payload_b64, signature_b64)):
            raise InvalidToken("token segment is not base64url")
        try:
            header = json.loads(_b64decode(header_b64))
            payload = json.loads(_b64decode(payload_b64))
        except (ValueError, UnicodeDecodeError):
            raise InvalidToken("token header or payload is not JSON") from None
        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise InvalidToken("token header and payload must be objects")
        alg = header.get("alg")
        if alg not in self.config.algorithms:
            raise InvalidToken("token algorithm is not accepted")
        if header.get("crit"):
            raise InvalidToken("critical header extensions are not supported")
        typ = str(header.get("typ", "at+jwt")).lower()
        if typ not in ("at+jwt", "application/at+jwt", "jwt"):
            raise InvalidToken("token type is not an access token")
        kid = header.get("kid")
        if kid is not None and not isinstance(kid, str):
            raise InvalidToken("token key id must be a string")
        jwk = self._keys.key_for(kid, alg)
        _verify_signature(alg, jwk, (header_b64 + "." + payload_b64).encode("ascii"), _b64decode(signature_b64))
        return self._claims(payload, required_scopes)

    def _claims(self, payload: dict, required_scopes: tuple[str, ...]) -> Claims:
        now, leeway = int(self._clock()), self.config.leeway
        if payload.get("iss") != self.config.issuer:
            raise InvalidToken("token issuer does not match")
        audience = payload.get("aud")
        audiences = [audience] if isinstance(audience, str) else audience if isinstance(audience, list) else []
        if self.config.audience not in audiences:
            raise InvalidToken("token audience does not include this resource")
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp + leeway <= now:
            raise InvalidToken("token is expired or has no expiry")
        for claim in ("nbf", "iat"):
            value = payload.get(claim)
            if value is not None and (not isinstance(value, (int, float)) or value - leeway > now):
                raise InvalidToken(f"token {claim} is in the future")
        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 256 or \
                any(ord(c) < 32 for c in subject):
            raise InvalidToken("token subject is missing or malformed")
        raw_scope = payload.get("scope", payload.get("scp", ""))
        if isinstance(raw_scope, str):
            scopes = frozenset(raw_scope.split())
        elif isinstance(raw_scope, list) and all(isinstance(s, str) for s in raw_scope):
            scopes = frozenset(raw_scope)
        else:
            raise InvalidToken("token scope is malformed")
        missing = [s for s in required_scopes if s not in scopes]
        if missing:
            raise InvalidToken("token lacks a required scope")
        return Claims(subject=subject, scopes=scopes, issuer=self.config.issuer, expires_at=int(exp), raw=payload)


def www_authenticate(metadata_url: str | None, *, error: str | None = None) -> str:
    """The 401 challenge, pointing clients at the protected-resource metadata."""
    parts = ['Bearer realm="commontrace"']
    if error:
        parts.append(f'error="{error}"')
    if metadata_url:
        parts.append(f'resource_metadata="{metadata_url}"')
    return ", ".join(parts)


_SERVERS: dict[tuple, ResourceServer] = {}
_SERVERS_LOCK = threading.Lock()


def resource_server(root: str | None) -> ResourceServer | None:
    """A shared verifier for the current configuration (so the JWKS cache is reused)."""
    config = load_config(root)
    if config is None:
        return None
    with _SERVERS_LOCK:
        server = _SERVERS.get((config,))
        if server is None:
            server = _SERVERS[(config,)] = ResourceServer(config)
        return server


WELL_KNOWN = "/.well-known/oauth-protected-resource"


def metadata_paths(config: OAuthConfig) -> tuple[str, ...]:
    """Paths serving the metadata: the bare well-known path, and (RFC 9728 section 3.1)
    the well-known path followed by the resource's own path, when it has one."""
    from urllib.parse import urlsplit

    path = urlsplit(config.audience).path.rstrip("/")
    return (WELL_KNOWN, WELL_KNOWN + path) if path else (WELL_KNOWN,)


def metadata_url(config: OAuthConfig) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(config.audience)
    return f"{parts.scheme}://{parts.netloc}{metadata_paths(config)[-1]}"
