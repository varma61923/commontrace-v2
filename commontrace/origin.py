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


def bind(record: dict, principal: Principal) -> dict:
    if len(principal.key) < 32:
        raise ValueError("origin signing key must be at least 32 bytes")
    body = {"record": record, "principal": principal.id, "organization": principal.organization,
            "authority": principal.authority, "digest": hashlib.sha256(_bytes(record)).hexdigest()}
    return {**body, "signature": hmac.new(principal.key, _bytes(body), hashlib.sha256).hexdigest()}


def verify(receipt: dict, principals: dict[str, Principal], *, authority: str | None = None) -> bool:
    try:
        principal = principals[receipt["principal"]]
        body = {key: value for key, value in receipt.items() if key != "signature"}
        expected = hmac.new(principal.key, _bytes(body), hashlib.sha256).hexdigest()
        return (hmac.compare_digest(expected, receipt["signature"])
                and receipt["organization"] == principal.organization
                and receipt["authority"] == principal.authority
                and (authority is None or authority == principal.authority)
                and receipt["digest"] == hashlib.sha256(_bytes(receipt["record"])).hexdigest())
    except (KeyError, TypeError, ValueError):
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
