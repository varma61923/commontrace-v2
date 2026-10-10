"""Origin-bound HMAC receipts for configured principals, not trust-by-content.

Keys come from a trusted operator, never from a self-signup or memory payload.
This binds origin and authority to immutable bytes; it does not prove truth or
defend against a compromised signer. Distinct principals are not distinct orgs.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass


def _bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


@dataclass(frozen=True)
class Principal:
    id: str
    organization: str
    authority: str
    key: bytes
    algorithm: str = "hmac-sha256"
    public_key: bytes = b""


def bind(record: dict, principal: Principal) -> dict:
    if len(principal.key) < 32:
        raise ValueError("origin signing key must be at least 32 bytes")
    # Detach shared object identities: strict Markdown frontmatter forbids YAML aliases.
    body = {"record": json.loads(_bytes(record)), "principal": principal.id, "organization": principal.organization,
            "authority": principal.authority, "digest": hashlib.sha256(_bytes(record)).hexdigest()}
    if principal.algorithm == "ed25519":
        import base64

        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            raise RuntimeError("Ed25519 signing requires commontrace[security]") from None
        body["algorithm"] = "ed25519"
        signature = Ed25519PrivateKey.from_private_bytes(principal.key).sign(_bytes(body))
        return {**body, "signature": base64.b64encode(signature).decode("ascii")}
    if principal.algorithm != "hmac-sha256":
        raise ValueError("unsupported signing algorithm")
    return {**body, "signature": hmac.new(principal.key, _bytes(body), hashlib.sha256).hexdigest()}


def verify(receipt: dict, principals: dict[str, Principal], *, authority: str | None = None) -> bool:
    try:
        principal = principals[receipt["principal"]]
        body = {key: value for key, value in receipt.items() if key != "signature"}
        if receipt.get("algorithm", "hmac-sha256") != principal.algorithm:
            return False
        if principal.algorithm == "ed25519":
            import base64

            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            try:
                Ed25519PublicKey.from_public_bytes(principal.public_key).verify(
                    base64.b64decode(receipt["signature"], validate=True), _bytes(body))
            except Exception:
                return False
            signature_valid = True
        else:
            expected = hmac.new(principal.key, _bytes(body), hashlib.sha256).hexdigest()
            signature_valid = hmac.compare_digest(expected, receipt["signature"])
        return (signature_valid
                and receipt["organization"] == principal.organization
                and receipt["authority"] == principal.authority
                and (authority is None or authority == principal.authority)
                and receipt["digest"] == hashlib.sha256(_bytes(receipt["record"])).hexdigest())
    except (KeyError, TypeError, ValueError, ImportError):
        return False


def corroboration(receipts: list[dict], principals: dict[str, Principal], *, digest: str) -> int:
    """Count configured independent organizations, not self-created identities."""
    return len({r["organization"] for r in receipts if r.get("digest") == digest and verify(r, principals)})


def collusion_signals(votes: list[dict], *, agreement_threshold: float = 0.95) -> list[dict]:
    """Descriptive co-voting flags requiring operator investigation, never guilt."""
    if not math.isfinite(agreement_threshold) or not 0 <= agreement_threshold <= 1:
        raise ValueError("agreement threshold must be in [0, 1]")
    by_principal: dict[str, dict] = {}
    for vote in votes:
        by_principal.setdefault(vote["principal"], {})[vote["memory_id"]] = vote["value"]
    result = []
    names = sorted(by_principal)
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            shared = set(by_principal[first]) & set(by_principal[second])
            if len(shared) < 5:
                continue
            agreement = sum(by_principal[first][mid] == by_principal[second][mid] for mid in shared) / len(shared)
            if agreement >= agreement_threshold:
                result.append({"principals": [first, second], "shared": len(shared), "agreement": agreement})
    return result
