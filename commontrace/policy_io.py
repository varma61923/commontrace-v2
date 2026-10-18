"""Operator-reviewed finite memory policy changes and canonical enforcement.

Certificates bind both complete finite policies and the active generation.
Activation rechecks the exhaustive difference under a local writer lock. New
stores keep their existing behavior until a managed policy is explicitly active.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Iterator

from commontrace import frontier_protocol as fp
from commontrace import policy_proof as pure
from commontrace import transparency_log as log
from commontrace.runtime_cache import RuntimeCache

ACTIVATIONS = "memory-policy-activations.jsonl"
_ADMIN: ContextVar[bool] = ContextVar("unimem_memory_policy_admin", default=False)
_ACTION: ContextVar[str] = ContextVar("unimem_memory_policy_action", default="read")
_CACHE: RuntimeCache[dict[str, Any]] = RuntimeCache(max_entries=32, max_bytes=8*fp.MAX_BYTES, ttl=300,
                                   weigh=lambda key, value: len(fp.wire(value))+len(repr(key)))


def _store() -> Any:
    from commontrace import nextwave_store

    return nextwave_store


@contextmanager
def _administration() -> Iterator[None]:
    _store().operator()
    token = _ADMIN.set(True)
    try:
        yield
    finally:
        _ADMIN.reset(token)


def enabled(root: str) -> bool:
    return os.path.isfile(fp.filename(root, ACTIVATIONS))


def _active(root: str) -> dict[str, Any] | None:
    if not enabled(root):
        return None
    count, tip = fp.checkpoint(root, ACTIVATIONS)
    if not count:
        raise PermissionError("UniMem active memory policy journal is empty or truncated")

    def load() -> dict[str, Any]:
        row = dict(fp.signed_at(root, ACTIVATIONS, count-1)["record"])
        policy = pure.normalize(row["policy"])
        if fp.digest(policy) != row["policy_hash"] or row["generation"] != count:
            raise PermissionError("UniMem active policy commitment or generation does not match")
        return {"policy": policy, "policy_hash": row["policy_hash"], "generation": count}

    return dict(_CACHE.get_or_load((os.path.realpath(root), count, tip), load))


def current(root: str) -> dict[str, Any] | None:
    _store().operator()
    value = _active(root)
    return json.loads(fp.wire(value)) if value is not None else None


def _candidate(policy: Any, format: str, domain: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if format == "unimem":
        if not isinstance(policy, dict) or domain is not None:
            raise ValueError("UniMem native policy must contain its own complete finite domain")
        return pure.normalize(policy), None
    if format != "cedar" or not isinstance(policy, str) or domain is None:
        raise ValueError("UniMem Cedar import requires policy text and an explicit finite domain")
    from commontrace import policy_cedar

    imported = policy_cedar.import_policy(policy, domain)
    return dict(imported["policy"]), dict(imported["analysis"])


def diff(root: str, policy: Any, *, format: str = "unimem", domain: dict[str, Any] | None = None) -> dict[str, Any]:
    _store().operator()
    proposed, analysis = _candidate(policy, format, domain)
    active = _active(root)
    before = active["policy"] if active else pure.deny(proposed["domain"])
    return {"difference": pure.difference(before, proposed), "generation": active["generation"] if active else 0,
            "proposed": proposed, "cedar_analysis": analysis,
            "bootstrap": "first managed allow requires review; unconfigured legacy stores are unchanged"}


def certify(root: str, policy: Any, *, format: str = "unimem", domain: dict[str, Any] | None = None,
            expires_seconds: int = 3600) -> dict[str, Any]:
    from commontrace import _jsonl

    store = _store()
    with _administration(), _jsonl.locked(fp.filename(root, "memory-policy.lock")):
        fp.count(expires_seconds, 60, 86400)
        result = diff(root, policy, format=format, domain=domain)
        if len(fp.wire(result["proposed"])) > 128*1024:
            raise ValueError("UniMem finite policy exceeds 128 KiB")
        new_hash = result["difference"]["new_hash"]
        store.put(root, "memory-policies", new_hash, result["proposed"], immutable=True)
        issued = fp.now()
        signed = fp.sign(root, {"type": "unimem.policy.change", "version": 1,
            "difference": result["difference"], "generation": result["generation"],
            "cedar_analysis": result["cedar_analysis"], "issued_at": issued,
            "expires_at": (datetime.fromisoformat(issued)+timedelta(seconds=expires_seconds)).isoformat()})
        anchor = log.append(root, "memory-policy-change", {"certificate_digest": fp.digest(signed),
            "old_hash": result["difference"]["old_hash"], "new_hash": new_hash,
            "no_widening": result["difference"]["no_widening"],
            "tuples_checked": result["difference"]["tuples_checked"]})
    return {"certificate": signed, "anchor": anchor}


def _certificate(root: str, certificate: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    fp.wire(certificate)
    signed, anchor = certificate["certificate"], certificate["anchor"]
    body = signed.get("record", {})
    if (not fp.verify(root, signed) or signed.get("authority") != "operator"
            or body.get("type") != "unimem.policy.change" or body.get("version") != 1
            or not log.verify(root, anchor)
            or anchor["statement"]["record"]["event"] != "memory-policy-change"
            or anchor["statement"]["record"]["statement"].get("certificate_digest") != fp.digest(signed)
            or not fp.moment(body["issued_at"]) <= fp.now() < fp.moment(body["expires_at"])):
        raise PermissionError("UniMem policy certificate is forged, expired or unanchored")
    proposed = _store().get(root, "memory-policies", body["difference"]["new_hash"])
    current = _active(root)
    before = current["policy"] if current else pure.deny(proposed["domain"])
    generation = current["generation"] if current else 0
    if generation != body["generation"] or pure.difference(before, proposed) != body["difference"]:
        raise PermissionError("UniMem policy certificate is stale or differs from its exhaustive recomputation")
    return dict(body), dict(proposed)


def review(root: str, certificate: dict[str, Any], *, reason: str, expires_seconds: int = 3600) -> dict[str, Any]:
    from commontrace import memory_authority

    with _administration():
        body, _ = _certificate(root, certificate)
        fp.text(reason, "policy review rationale", 2000)
        fp.count(expires_seconds, 60, 86400)
        issued = fp.now()
        signed = fp.sign(root, {"type": "unimem.policy.widening-review", "version": 1,
            "certificate_digest": fp.digest(certificate["certificate"]),
            "old_hash": body["difference"]["old_hash"], "new_hash": body["difference"]["new_hash"],
            "generation": body["generation"], "reviewer": memory_authority.bound_writer()[0],
            "reason_digest": fp.digest(reason), "issued_at": issued,
            "expires_at": (datetime.fromisoformat(issued)+timedelta(seconds=expires_seconds)).isoformat()})
        anchor = log.append(root, "memory-policy-review", {"review_digest": fp.digest(signed)})
        return {"receipt": signed, "anchor": anchor}


def activate(root: str, certificate: dict[str, Any], *, review_receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    from commontrace import _jsonl

    with _administration(), _jsonl.locked(fp.filename(root, "memory-policy.lock")):
        body, proposed = _certificate(root, certificate)
        if not body["difference"]["no_widening"]:
            if review_receipt is None:
                raise PermissionError("UniMem widening policy requires an explicit content-bound operator review")
            fp.wire(review_receipt)
            signed, anchor = review_receipt["receipt"], review_receipt["anchor"]
            receipt = signed["record"]
            if (not fp.verify(root, signed) or signed.get("authority") != "operator"
                    or receipt.get("type") != "unimem.policy.widening-review" or receipt.get("version") != 1
                    or not log.verify(root, anchor)
                    or anchor["statement"]["record"]["event"] != "memory-policy-review"
                    or anchor["statement"]["record"]["statement"].get("review_digest") != fp.digest(signed)
                    or receipt["certificate_digest"] != fp.digest(certificate["certificate"])
                    or any(receipt[key] != body["difference"][key] for key in ("old_hash", "new_hash"))
                    or receipt["generation"] != body["generation"]
                    or not fp.moment(receipt["issued_at"]) <= fp.now() < fp.moment(receipt["expires_at"])):
                raise PermissionError("UniMem policy widening review is stale, forged, expired or for other content")
        signed = fp.append(root, ACTIVATIONS, {"generation": body["generation"]+1,
            "policy": proposed, "policy_hash": body["difference"]["new_hash"],
            "certificate_digest": fp.digest(certificate["certificate"]),
            "review_digest": fp.digest(review_receipt) if review_receipt else None})
        anchor = log.append(root, "memory-policy-activate", {"activation_digest": fp.digest(signed)})
        return {"activation": signed, "anchor": anchor}


@contextmanager
def authorization(action: str) -> Iterator[None]:
    """The application operation, rather than memory content, selects the action."""
    if action not in pure.ACTIONS:
        raise ValueError("UniMem unknown policy action")
    token = _ACTION.set(action)
    try:
        yield
    finally:
        _ACTION.reset(token)


def permits(root: str, record: Mapping[str, Any], *, action: str | None = None) -> bool:
    """Application context binds principal and purpose; payloads cannot choose them."""
    from commontrace import purpose, subject_policy

    if not subject_policy.permits(root, record):
        return False
    if not purpose.permits(root, record, action=action or _ACTION.get()):
        return False
    if not enabled(root):
        return True
    if _ADMIN.get() and record.get("kind") == "memory-policies":
        return True
    from commontrace import capabilities, capability_io, memory_authority

    current = _active(root)
    if current is None:
        return False
    principal, authority = memory_authority.bound_writer()
    delegation = capability_io.current(root) if capabilities.CURRENT.get() is not None else None
    processing_purpose = delegation["purpose"] if delegation else purpose.current(root) or "default"
    labels = record.get("scopes", [])
    memory_class = record.get("memory_type") or record.get("memory_class") or record.get("kind")
    if not memory_class:
        memory_class = "trace" if memory_authority.record_class(dict(record)) == "trace" else "general"
    if (not isinstance(labels, list) or len(labels) > 128
            or any(not isinstance(label, str) for label in labels) or not isinstance(memory_class, str)):
        return False
    return pure._allows(current["policy"], authority+":"+principal, action or _ACTION.get(), memory_class,
                        tuple(sorted(set(labels))), processing_purpose)


def assert_write(root: str, record: Mapping[str, Any]) -> None:
    from commontrace import subject_policy

    subject_policy.assert_write(root, record)
    if not permits(root, record, action="write"):
        raise PermissionError("UniMem active finite policy denies this memory write")


@contextmanager
def fact_mutations(root: str, facts: Mapping[str, Any]) -> Iterator[None]:
    """Guard legacy transactions while preserving independent owner erasure."""
    from commontrace import subject_policy

    if (not enabled(root) and not os.path.isfile(fp.filename(root, "consent-state.json"))
            and not subject_policy.enabled(root)):
        yield
        return
    before = {identity: json.loads(fp.wire(fact.to_dict())) for identity, fact in facts.items()}
    yield
    for identity, fact in facts.items():
        after = fact.to_dict()
        if before.get(identity) != after:
            if identity in before:
                assert_write(root, before[identity])
            assert_write(root, after)


def assert_export(root: str, identities: list[str], *, context: list[str] | None = None) -> None:
    if not enabled(root) and not os.path.isfile(fp.filename(root, "consent-state.json")):
        return
    store = _store()
    fp.count(len(identities), 0, 512)
    with authorization("export"):
        canonical = [identity for identity in identities if not identity.startswith("artifact:")]
        if set(store.admit(root, canonical, context=context or [])) != set(canonical):
            raise PermissionError("UniMem active finite policy denies one or more export sources")
        for identity in identities:
            if identity.startswith("artifact:"):
                _, kind, key = identity.split(":", 2)
                store.get(root, kind, key, context=context or [])
