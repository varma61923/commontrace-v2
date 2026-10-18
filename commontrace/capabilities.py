"""UniMem bounded attenuating capabilities and request proofs of possession.

This is a UniMem encoding inspired by draft-niyikiza-oauth-attenuating-agent-
tokens-01, not JWT/AAT wire compatibility. Authentication pins keys externally.
Local links require both the previous MAC and the parent's private holder key:
portable prefix MACs alone cannot authorize an alternate delegation branch.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any

from commontrace import frontier_protocol as fp

OPERATIONS = frozenset({"read", "write", "forget", "export"})
ALGORITHMS = frozenset({"hmac-sha256", "ed25519"})
SET_FIELDS = ("namespaces", "operations", "classes", "purposes", "labels", "delegates")
FIELDS = frozenset((*SET_FIELDS, "ceiling", "audience", "expires_at"))
CURRENT: ContextVar[dict[str, Any] | None] = ContextVar("unimem_capability", default=None)
STICKY: ContextVar[tuple[str, ...]] = ContextVar("unimem_handoff_labels", default=())


def _label(value: Any) -> str:
    label = fp.text(value, "capability label", 256)
    if label != label.strip() or any(ord(char) < 32 or ord(char) == 127 for char in label) or "*" in label:
        raise ValueError("UniMem capability labels must be exact printable names")
    return label


def moment(value: str) -> datetime:
    return datetime.fromisoformat(fp.moment(value))


def policy(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != FIELDS:
        raise ValueError("UniMem capability caveats have unknown or missing fields")
    result: dict[str, Any] = {}
    for field in SET_FIELDS:
        items = value[field]
        if not isinstance(items, list) or len(items) > 128 or (not items and field not in {"labels", "delegates"}):
            raise ValueError("UniMem capability caveats require bounded explicit lists")
        cleaned = sorted({_label(item) for item in items})
        if len(cleaned) != len(items):
            raise ValueError("UniMem duplicate capability caveat")
        result[field] = cleaned
    if not set(result["operations"]) <= OPERATIONS:
        raise ValueError("UniMem unsupported delegated operation")
    result["ceiling"] = fp.count(value["ceiling"], 0, 3, "integrity ceiling")
    result["audience"] = _label(value["audience"])
    result["expires_at"] = moment(value["expires_at"]).isoformat()
    return result


def subset(parent: Mapping[str, Any], child: Mapping[str, Any]) -> None:
    before, after = policy(parent), policy(child)
    if any(not set(after[field]) <= set(before[field]) for field in SET_FIELDS):
        raise PermissionError("UniMem delegated caveats cannot widen")
    if after["ceiling"] > before["ceiling"] or moment(after["expires_at"]) > moment(before["expires_at"]):
        raise PermissionError("UniMem delegated ceiling or expiry cannot widen")
    if after["audience"] != before["audience"] and after["audience"] not in before["delegates"]:
        raise PermissionError("UniMem audience is not an authorized delegate")


def _bytes(domain: str, record: Mapping[str, Any]) -> bytes:
    return domain.encode() + b"\x00" + fp.wire(dict(record))


def _sign(algorithm: str, key: bytes, data: bytes) -> str:
    if algorithm not in ALGORITHMS or not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("UniMem capability signing requires a pinned algorithm and 32-byte key")
    if algorithm == "hmac-sha256":
        return hmac.new(key, data, hashlib.sha256).hexdigest()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.from_private_bytes(key).sign(data).hex()


def _verify(algorithm: str, key: bytes, data: bytes, signature: str) -> None:
    if algorithm not in ALGORITHMS or not isinstance(key, bytes) or len(key) != 32 or not isinstance(signature, str):
        raise PermissionError("UniMem invalid pinned verification key or algorithm")
    if algorithm == "hmac-sha256":
        if len(signature) != 64 or not hmac.compare_digest(_sign(algorithm, key, data), signature):
            raise PermissionError("UniMem capability signature rejected")
        return
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(key).verify(bytes.fromhex(signature), data)
    except (InvalidSignature, ValueError):
        raise PermissionError("UniMem capability signature rejected") from None


def _token(token: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(token, Mapping) or set(token) != {"version", "algorithm", "links"}:
        raise ValueError("UniMem invalid capability envelope")
    if type(token["version"]) is not int or token["version"] != 1 or token["algorithm"] not in ALGORITHMS:
        raise ValueError("UniMem unsupported capability version or algorithm")
    links = token["links"]
    if not isinstance(links, list) or not 1 <= len(links) <= 16 or len(fp.wire(dict(token))) > 65536:
        raise ValueError("UniMem capability exceeds its chain bounds")
    for node in links:
        if not isinstance(node, dict) or set(node) != {"record", "signature", "authorization"}:
            raise ValueError("UniMem invalid capability chain node")
        if not isinstance(node["record"], dict):
            raise ValueError("UniMem invalid capability node record")
    return links


def _prefix(token: Mapping[str, Any], count: int) -> dict[str, Any]:
    return {"version": 1, "algorithm": token["algorithm"], "links": token["links"][:count]}


def grant(caveats: Mapping[str, Any], *, issuer: str, tenant: str, nonce: str,
          now: str, algorithm: str, key: bytes) -> dict[str, Any]:
    caveats = policy(caveats)
    issued = moment(now)
    if not issued < moment(caveats["expires_at"]) <= issued + timedelta(days=7):
        raise ValueError("UniMem root capability lifetime must be positive and at most seven days")
    record = {"type": "unimem.capability.root.v1", "issuer": _label(issuer), "tenant": _label(tenant),
              "nonce": _label(nonce), "issued_at": issued.isoformat(), "caveats": caveats}
    return {"version": 1, "algorithm": algorithm, "links": [{"record": record,
        "signature": _sign(algorithm, key, _bytes("unimem.capability.root.v1", record)), "authorization": ""}]}


def attenuate(token: Mapping[str, Any], caveats: Mapping[str, Any], *, key: bytes) -> dict[str, Any]:
    links = _token(token)
    if len(links) >= 16:
        raise ValueError("UniMem capability chain depth exhausted")
    caveats = policy(caveats)
    subset(links[-1]["record"]["caveats"], caveats)
    record = {"type": "unimem.capability.child.v1", "parent": fp.digest(dict(token)), "caveats": caveats}
    data = _bytes("unimem.capability.child.v1", record)
    if token["algorithm"] == "hmac-sha256":
        signature = _sign("hmac-sha256", bytes.fromhex(links[-1]["signature"]), data)
        authorization = _sign("hmac-sha256", key, _bytes("unimem.capability.parent-holder.v1",
                                                       {"record": record, "signature": signature}))
    else:
        signature, authorization = _sign("ed25519", key, data), ""
    result = {**dict(token), "links": [*links, {"record": record, "signature": signature,
                                               "authorization": authorization}]}
    _token(result)
    return result


def verify(token: Mapping[str, Any], *, issuer: str, tenant: str, algorithm: str, key: bytes,
           holder_key: Callable[[str], bytes], now: str, revoked: Sequence[str] = ()) -> dict[str, Any]:
    links = _token(token)
    if token["algorithm"] != algorithm:
        raise PermissionError("UniMem capability algorithm differs from issuer pin")
    root = links[0]["record"]
    if set(root) != {"type", "issuer", "tenant", "nonce", "issued_at", "caveats"} or root.get(
            "type") != "unimem.capability.root.v1" or root["issuer"] != issuer or root["tenant"] != tenant:
        raise PermissionError("UniMem capability issuer or tenant binding rejected")
    issued, stamp = moment(root["issued_at"]), moment(now)
    root_policy = policy(root["caveats"])
    if issued > stamp or not issued < moment(root_policy["expires_at"]) <= issued + timedelta(days=7):
        raise PermissionError("UniMem capability issuance or lifetime rejected")
    _label(root["nonce"])
    if links[0]["authorization"] != "":
        raise ValueError("UniMem root has no parent authorization")
    _verify(algorithm, key, _bytes("unimem.capability.root.v1", root), links[0]["signature"])
    chain_ids = []
    for index, node in enumerate(links):
        record = node["record"]
        current_policy = policy(record["caveats"])
        if moment(current_policy["expires_at"]) <= stamp:
            raise PermissionError("UniMem capability expired")
        if index:
            if set(record) != {"type", "parent", "caveats"} or record.get("type") != "unimem.capability.child.v1":
                raise ValueError("UniMem invalid child record")
            if record["parent"] != fp.digest(_prefix(token, index)):
                raise PermissionError("UniMem capability parent binding rejected")
            parent = policy(links[index - 1]["record"]["caveats"])
            subset(parent, current_policy)
            data = _bytes("unimem.capability.child.v1", record)
            holder = holder_key(parent["audience"])
            if algorithm == "hmac-sha256":
                _verify(algorithm, bytes.fromhex(links[index - 1]["signature"]), data, node["signature"])
                _verify(algorithm, holder, _bytes("unimem.capability.parent-holder.v1",
                                                {"record": record, "signature": node["signature"]}),
                        node["authorization"])
            else:
                if node["authorization"] != "":
                    raise ValueError("UniMem Ed25519 link has an unexpected local authorization")
                _verify(algorithm, holder, data, node["signature"])
        chain_id = fp.digest(_prefix(token, index + 1))
        if chain_id in revoked:
            raise PermissionError("UniMem capability chain was revoked")
        chain_ids.append(chain_id)
    return {"tenant": tenant, "issuer": issuer, "caveats": policy(links[-1]["record"]["caveats"]),
            "chain_ids": chain_ids, "chain_digest": fp.digest(dict(token))}


def permits(caveats: Mapping[str, Any], *, namespace: str, operation: str, memory_class: str,
            purpose: str, labels: Sequence[str]) -> bool:
    rules = policy(caveats)
    return bool(namespace in rules["namespaces"] and operation in rules["operations"]
                and memory_class in rules["classes"] and purpose in rules["purposes"]
                and set(labels) <= set(rules["labels"]))


def request_digest(method: str, target: str, raw: bytes, *, purpose: str) -> str:
    return fp.digest({"method": _label(method), "target": fp.text(target, "request target", 8192),
                      "body_sha256": hashlib.sha256(raw).hexdigest(), "purpose": _label(purpose)})


def prove(token: Mapping[str, Any], *, key: bytes, algorithm: str, audience: str, tenant: str,
          request_digest: str, nonce: str, now: str) -> dict[str, Any]:
    _token(token)
    issued = moment(now)
    record = {"type": "unimem.capability.possession.v1", "chain_digest": fp.digest(dict(token)),
              "audience": _label(audience), "tenant": _label(tenant), "request_digest": _label(request_digest),
              "nonce": _label(nonce), "issued_at": issued.isoformat(),
              "expires_at": (issued + timedelta(seconds=30)).isoformat()}
    return {"record": record, "signature": _sign(algorithm, key, _bytes("unimem.capability.possession.v1", record))}


def verify_proof(token: Mapping[str, Any], verified: Mapping[str, Any], proof: Mapping[str, Any], *,
                 key: bytes, algorithm: str, request_digest: str, now: str) -> str:
    if not isinstance(proof, Mapping) or set(proof) != {"record", "signature"}:
        raise ValueError("UniMem invalid possession proof")
    record = proof["record"]
    fields = {"type", "chain_digest", "audience", "tenant", "request_digest", "nonce", "issued_at", "expires_at"}
    if not isinstance(record, dict) or set(record) != fields or len(fp.wire(dict(proof))) > 4096:
        raise ValueError("UniMem invalid possession proof fields")
    issued, expiry, stamp = moment(record["issued_at"]), moment(record["expires_at"]), moment(now)
    if (record["type"] != "unimem.capability.possession.v1" or not issued <= stamp < expiry
            or expiry > issued + timedelta(seconds=30) or record["chain_digest"] != fp.digest(dict(token))
            or record["audience"] != verified["caveats"]["audience"] or record["tenant"] != verified["tenant"]
            or record["request_digest"] != request_digest):
        raise PermissionError("UniMem possession proof binding or freshness rejected")
    _verify(algorithm, key, _bytes("unimem.capability.possession.v1", record), proof["signature"])
    return _label(record["nonce"])


def headers(token: Mapping[str, Any], *, key: bytes, holder_algorithm: str, method: str, target: str,
            raw: bytes, purpose: str, nonce: str, now: str) -> dict[str, str]:
    links = _token(token)
    proof = prove(token, key=key, algorithm=holder_algorithm, audience=links[-1]["record"]["caveats"]["audience"],
                  tenant=links[0]["record"]["tenant"],
                  request_digest=request_digest(method, target, raw, purpose=purpose),
                  nonce=nonce, now=now)
    return {"X-UniMem-Capability": base64.urlsafe_b64encode(fp.wire(dict(token))).decode().rstrip("="),
            "X-UniMem-Capability-Proof": base64.urlsafe_b64encode(fp.wire(proof)).decode().rstrip("="),
            "X-UniMem-Purpose": purpose}


def header_parameters() -> list[dict[str, Any]]:
    """Optional transport metadata; primary authentication remains required."""
    return [{"in": "header", "name": name, "required": False, "description": description,
             "schema": {"type": "string", "minLength": 1, "maxLength": bound}} for name, bound, description in (
        ("X-UniMem-Capability", 98304,
         "Base64url UniMem attenuating token. Enrolled delegates must supply all three UniMem headers in addition "
         "to primary authentication; an unconfigured server rejects delegated requests."),
        ("X-UniMem-Capability-Proof", 8192,
         "Base64url holder signature bound to the token, tenant, exact method/target/body, purpose and fresh nonce. "
         "Regenerate for every HTTP request; proofs expire after at most 30 seconds."),
        ("X-UniMem-Purpose", 256, "Declared purpose, authorized by the leaf capability and signed by the holder."),
    )]
