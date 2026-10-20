"""W3C VC 2.0 SD-JWT credentials with pinned Ed25519 issuers and holder binding.

RFC 9901 object disclosures, SHA-256, mandatory KB-JWT, and the vc+sd-jwt
media type from VC JOSE/COSE. This bounded profile supports one selectively
 disclosed credentialSubject object; recursive and array disclosures are rejected.
Cryptography is imported only when signing or verifying (the security extra).
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from commontrace import frontier_protocol as fp

MAX_TOKEN = 128*1024
STATUS_BITS = 131072
VC_CONTEXT = "https://www.w3.org/ns/credentials/v2"
CLAIMS = frozenset({"statement", "confidence", "sourceId", "provenance", "labels", "purposes", "consent",
                   "createdAt", "updatedAt", "modelDigest", "subjectId", "memoryValidFrom", "memoryValidUntil",
                   "memoryClass", "revision", "inferred"})
IssuerKeys = Mapping[tuple[str, str], bytes]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value) or len(value) > MAX_TOKEN:
        raise ValueError("UniMem invalid bounded base64url value")
    result = base64.b64decode(value+"="*(-len(value) % 4), altchars=b"-_", validate=True)
    if _b64(result) != value:
        raise ValueError("UniMem noncanonical base64url value")
    return result


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("UniMem duplicate credential JSON key")
        result[name] = value
    return result


def _json(raw: bytes) -> Any:
    if len(raw) > MAX_TOKEN:
        raise ValueError("UniMem credential JSON exceeds its bound")
    def constant(value: str) -> None:
        raise ValueError("UniMem credential JSON must contain finite numbers")
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=constant)


def public_key(private: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return bytes(Ed25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw())


def _sign(payload: dict[str, Any], private: bytes, *, typ: str, kid: str = "") -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    header = {"alg": "EdDSA", "typ": typ, **({"kid": fp.text(kid, "credential key id", 256)} if kid else {})}
    encoded = _b64(fp.wire(header))+"."+_b64(fp.wire(payload))
    token = encoded+"."+_b64(Ed25519PrivateKey.from_private_bytes(private).sign(encoded.encode("ascii")))
    fp.text(token, "credential token", MAX_TOKEN)
    return token


def _parts(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    fp.text(token, "credential token", MAX_TOKEN)
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("UniMem expected a compact signed credential")
    header, payload = _json(_unb64(parts[0])), _json(_unb64(parts[1]))
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise ValueError("UniMem credential header and payload must be objects")
    return header, payload, (parts[0]+"."+parts[1]).encode("ascii"), _unb64(parts[2])


def _verified(token: str, public: bytes, typ: str) -> tuple[dict[str, Any], dict[str, Any]]:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    header, payload, signed, signature = _parts(token)
    if header.get("alg") != "EdDSA" or header.get("typ") != typ or set(header)-{"alg", "typ", "kid"}:
        raise PermissionError("UniMem unsupported credential algorithm, type or critical header")
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, signed)
    except (InvalidSignature, ValueError):
        raise PermissionError("UniMem credential signature does not match its pinned key") from None
    return header, payload


def _instant(timestamp: int) -> str:
    fp.count(timestamp, 0, 2**40, "credential time")
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _temporal(payload: Mapping[str, Any], now: int) -> None:
    fp.count(now, 0, 2**40)
    issued, expires = payload["iat"], payload["exp"]
    fp.count(issued, 0, 2**40)
    fp.count(expires, issued+1, issued+86400*365)
    if (not issued <= now < expires or payload.get("nbf") != issued
            or payload.get("validFrom") != _instant(issued) or payload.get("validUntil") != _instant(expires)):
        raise PermissionError("UniMem credential is not valid at this time")


def _base(issuer: str, identity: str, issued_at: int, expires_at: int) -> dict[str, Any]:
    fp.count(expires_at, issued_at+1, issued_at+86400*365)
    for value, label in ((issuer, "issuer"), (identity, "credential id")):
        fp.text(value, label, 512)
        if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
            raise ValueError("UniMem credential issuer and id must be absolute identifiers")
    return {"@context": [VC_CONTEXT], "id": identity, "type": ["VerifiableCredential"], "issuer": issuer,
            "iss": issuer, "iat": issued_at, "nbf": issued_at, "exp": expires_at,
            "validFrom": _instant(issued_at), "validUntil": _instant(expires_at)}


def _fact_context() -> dict[str, Any]:
    return {"MemoryFactCredential": "urn:unimem:credential:MemoryFactCredential",
            **{name: {"@id": "urn:unimem:memory:"+name, "@type": "@json"} for name in sorted(CLAIMS)}}


def issue(claims: Mapping[str, Any], *, issuer: str, kid: str, signing_key: bytes, holder_public: bytes,
          credential_id: str, status_url: str, status_index: int, issued_at: int, expires_at: int) -> str:
    """Return the issuer token and all disclosures to the holder, never a verifier."""
    fp.count(len(claims), 1, 16)
    if len(holder_public) != 32 or len(fp.wire(dict(claims))) > 48*1024:
        raise ValueError("UniMem holder key or fact claims exceed their bounds")
    fp.count(status_index, 0, STATUS_BITS-1)
    fp.text(status_url, "credential status URL", 512)
    parsed = urlsplit(status_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("UniMem status list needs an absolute HTTPS credential URL")
    context = _fact_context()
    disclosures, digests = [], []
    for name, value in sorted(claims.items()):
        if name not in CLAIMS:
            raise ValueError("UniMem unsupported selectively disclosed fact field")
        # Nested selective-disclosure markers would require a different profile.
        def inspect(item: Any, depth: int = 0) -> None:
            if depth > 8:
                raise ValueError("UniMem credential claim nesting exceeds its bound")
            if isinstance(item, dict):
                if any(not isinstance(key, str) or key.startswith("_") for key in item):
                    raise ValueError("UniMem recursive disclosure markers are unsupported")
                for child in item.values():
                    inspect(child, depth+1)
            elif isinstance(item, list):
                for child in item:
                    inspect(child, depth+1)
        inspect(value)
        disclosure = _b64(fp.wire([_b64(secrets.token_bytes(32)), name, value]))
        disclosures.append(disclosure)
        digests.append(_b64(hashlib.sha256(disclosure.encode("ascii")).digest()))
    # Decoys hide the exact number of fields in the bounded fact profile.
    while len(digests) < 16:
        digests.append(_b64(secrets.token_bytes(32)))
    secrets.SystemRandom().shuffle(digests)
    payload = _base(issuer, credential_id, issued_at, expires_at)
    payload["@context"].append(context)
    payload["type"].append("MemoryFactCredential")
    payload.update({"_sd_alg": "sha-256", "credentialSubject": {"_sd": digests},
        "cnf": {"jwk": {"kty": "OKP", "crv": "Ed25519", "x": _b64(holder_public)}},
        "credentialStatus": {"id": status_url+"#"+str(status_index), "type": "BitstringStatusListEntry",
            "statusPurpose": "revocation", "statusListIndex": str(status_index), "statusListCredential": status_url}})
    return _sign(payload, signing_key, typ="vc+sd-jwt", kid=kid)+"~"+"~".join(disclosures)+"~"


def present(issued: str, disclose: Sequence[str], *, holder_key: bytes,
            audience: str, nonce: str, issued_at: int) -> str:
    fp.text(issued, "issued SD-JWT", MAX_TOKEN)
    fp.text(audience, "credential audience", 512)
    fp.text(nonce, "verifier nonce", 256)
    parts = issued.split("~")
    if len(parts) < 2 or parts[-1] != "" or len(parts) > 18:
        raise ValueError("UniMem expected a bounded issued SD-JWT ending in a separator")
    _, payload, _, _ = _parts(parts[0])
    if _holder(payload) != public_key(holder_key):
        raise PermissionError("UniMem presentation key is not the issuer-bound holder")
    wanted = set(disclose)
    if isinstance(disclose, (str, bytes)) or len(wanted) != len(disclose):
        raise ValueError("UniMem disclosure selection must contain distinct field names")
    selected = []
    for disclosure in parts[1:-1]:
        decoded = _json(_unb64(disclosure))
        if not isinstance(decoded, list) or len(decoded) != 3:
            raise ValueError("UniMem unsupported fact disclosure")
        if decoded[1] in wanted:
            selected.append(disclosure)
            wanted.remove(decoded[1])
    if wanted:
        raise ValueError("UniMem selected a claim that is not in this credential")
    prefix = parts[0]+"~"+"".join(value+"~" for value in selected)
    binding = {"iat": issued_at, "aud": audience, "nonce": nonce,
               "sd_hash": _b64(hashlib.sha256(prefix.encode("ascii")).digest())}
    return prefix+_sign(binding, holder_key, typ="kb+jwt")


def _holder(payload: Mapping[str, Any]) -> bytes:
    jwk = payload["cnf"]["jwk"]
    if set(jwk) != {"kty", "crv", "x"} or jwk["kty"] != "OKP" or jwk["crv"] != "Ed25519":
        raise ValueError("UniMem unsupported holder-binding key")
    public = _unb64(jwk["x"])
    if len(public) != 32:
        raise ValueError("UniMem invalid holder public key")
    return public


def _issuer(token: str, keys: IssuerKeys, typ: str) -> dict[str, Any]:
    header, payload, _, _ = _parts(token)
    issuer, kid = payload.get("iss"), header.get("kid")
    if not isinstance(issuer, str) or not isinstance(kid, str):
        raise PermissionError("UniMem credential requires an explicit issuer and pinned key id")
    public = keys.get((issuer, kid))
    if public is None:
        raise PermissionError("UniMem credential issuer and key are not pinned")
    _, verified = _verified(token, public, typ)
    context = verified.get("@context")
    if (verified.get("issuer") != verified.get("iss") or not isinstance(context, list)
            or not context or context[0] != VC_CONTEXT):
        raise PermissionError("UniMem credential issuer or VC 2.0 context mismatch")
    return verified


def verify(presentation: str, *, issuer_keys: IssuerKeys, audience: str, nonce: str, now: int,
           is_revoked: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    """Verify a presentation; the caller must consume its own fresh verifier nonce."""
    fp.text(presentation, "credential presentation", MAX_TOKEN)
    fp.text(audience, "credential audience", 512)
    fp.text(nonce, "verifier nonce", 256)
    parts = presentation.split("~")
    if len(parts) < 2 or not parts[-1] or len(parts) > 18:
        raise PermissionError("UniMem a credential presentation requires holder key binding")
    payload = _issuer(parts[0], issuer_keys, "vc+sd-jwt")
    _temporal(payload, now)
    if (set(payload) != {"@context", "id", "type", "issuer", "iss", "iat", "nbf", "exp", "validFrom", "validUntil",
                         "_sd_alg", "credentialSubject", "cnf", "credentialStatus"}
            or payload.get("@context") != [VC_CONTEXT, _fact_context()]
            or payload.get("_sd_alg") != "sha-256"
            or payload.get("type") != ["VerifiableCredential", "MemoryFactCredential"]
            or set(payload["credentialSubject"]) != {"_sd"}):
        raise PermissionError("UniMem unsupported SD-JWT fact credential profile")
    digests = payload["credentialSubject"]["_sd"]
    if (not isinstance(digests, list) or len(digests) != 16 or len(set(digests)) != 16
            or any(len(_unb64(digest)) != 32 for digest in digests)):
        raise PermissionError("UniMem malformed disclosure commitments")
    disclosed: dict[str, Any] = {}
    seen = set()
    for value in parts[1:-1]:
        digest = _b64(hashlib.sha256(value.encode("ascii")).digest())
        disclosure = _json(_unb64(value))
        if (digest not in digests or digest in seen or not isinstance(disclosure, list) or len(disclosure) != 3
                or len(_unb64(disclosure[0])) < 16 or not isinstance(disclosure[1], str)
                or disclosure[1] in disclosed or disclosure[1] not in CLAIMS):
            raise PermissionError("UniMem invalid, repeated or uncommitted disclosure")
        seen.add(digest)
        disclosed[disclosure[1]] = disclosure[2]
    _, binding = _verified(parts[-1], _holder(payload), "kb+jwt")
    fp.count(binding["iat"], 0, 2**40)
    expected = _b64(hashlib.sha256(("~".join(parts[:-1])+"~").encode("ascii")).digest())
    if (set(binding) != {"iat", "aud", "nonce", "sd_hash"} or binding["aud"] != audience
            or binding["nonce"] != nonce or not now-60 <= binding["iat"] <= now+5
            or binding["iat"] < payload["iat"] or binding["iat"] >= payload["exp"]
            or not secrets.compare_digest(binding["sd_hash"], expected)):
        raise PermissionError("UniMem holder binding has the wrong nonce, audience, age or disclosure set")
    status = payload["credentialStatus"]
    if (status.get("type") != "BitstringStatusListEntry" or status.get("statusPurpose") != "revocation"
            or is_revoked(status) is not False):
        raise PermissionError("UniMem credential is revoked or its current status is unavailable")
    # No canonical lookup or undisclosed value appears in the verifier's output.
    return {"@context": payload["@context"], "id": payload["id"], "type": payload["type"],
            "issuer": payload["issuer"], "validFrom": payload["validFrom"], "validUntil": payload["validUntil"],
            "credentialSubject": disclosed, "credentialStatus": status, "holder_bound": True}


def status_list(revoked: Sequence[int], *, issuer: str, kid: str, signing_key: bytes, status_url: str,
                issued_at: int, expires_at: int, audience: str | None = None, nonce: str | None = None) -> str:
    bits = bytearray(STATUS_BITS//8)
    fp.count(len(revoked), 0, STATUS_BITS)
    for index in revoked:
        fp.count(index, 0, STATUS_BITS-1)
        bits[index//8] |= 1 << (7-index % 8)  # W3C index zero is the leftmost bit.
    payload = _base(issuer, status_url, issued_at, expires_at)
    payload["type"].append("BitstringStatusListCredential")
    payload["credentialSubject"] = {"id": status_url+"#list", "type": "BitstringStatusList",
        "statusPurpose": "revocation", "encodedList": "u"+_b64(gzip.compress(bytes(bits), mtime=0))}
    if audience is not None or nonce is not None:
        if audience is None or nonce is None:
            raise ValueError("UniMem a status freshness quote needs both verifier audience and nonce")
        payload["aud"] = fp.text(audience, "status verifier audience", 512)
        payload["nonce"] = fp.text(nonce, "status verifier nonce", 256)
    return _sign(payload, signing_key, typ="vc+jwt", kid=kid)


def revoked(status_token: str, entry: Mapping[str, Any], *, issuer_keys: IssuerKeys, issuer: str, now: int) -> bool:
    payload = _issuer(status_token, issuer_keys, "vc+jwt")
    _temporal(payload, now)
    subject = payload["credentialSubject"]
    index_text = entry["statusListIndex"]
    if not isinstance(index_text, str) or not re.fullmatch(r"0|[1-9][0-9]{0,5}", index_text):
        raise ValueError("UniMem invalid revocation status index")
    index = fp.count(int(index_text), 0, STATUS_BITS-1)
    url = entry["statusListCredential"]
    if (payload["issuer"] != issuer or payload["id"] != url or entry["id"] != url+"#"+index_text
            or payload["@context"] != [VC_CONTEXT]
            or payload["type"] != ["VerifiableCredential", "BitstringStatusListCredential"]
            or subject.get("type") != "BitstringStatusList" or subject.get("statusPurpose") != "revocation"
            or not subject.get("encodedList", "").startswith("u")):
        raise PermissionError("UniMem revocation list does not match the credential status entry")
    with gzip.GzipFile(fileobj=io.BytesIO(_unb64(subject["encodedList"][1:]))) as compressed:
        bits = compressed.read(STATUS_BITS//8+1)
    if len(bits) != STATUS_BITS//8:
        raise ValueError("UniMem revocation list has an unsupported size")
    return bool(bits[index//8] & (1 << (7-index % 8)))
