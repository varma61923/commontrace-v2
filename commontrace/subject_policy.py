"""Write-time subject rule packs with hash-only decision evidence.

These conservative operational rules are not a legal compliance certificate.
Subject class and deployment context come from operator configuration, never
from the proposed memory. Modality is an ingestion assertion, not sensor proof.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

from commontrace import _jsonl
from commontrace import frontier_protocol as fp

CLASSES = ("child-under-13", "adult", "care-resident")
CONTEXTS = ("workplace", "education", "home", "clinical")
MODALITIES = ("text", "voice", "face", "physiology", "image", "video", "tactile", "force", "position",
              "temperature", "system", "unknown")
BIOMETRIC = frozenset({"voice", "face", "physiology"})
NATIVE_KINDS = frozenset({"episode", "fact", "entity", "observation", "profile", "lesson", "directive"})
EMOTIONS = frozenset({"emotion", "emotional-state", "arousal", "valence", "happy", "sad", "angry", "stressed"})
PACKS = ("coppa-2025", "eu_ai_act_art5_1f")
RETENTION = "subject-retention.jsonl"
MEDICAL = "https://w3id.org/dpv#ImproveHealthcare"
SAFETY = "urn:unimem:purpose:safety"
_SOURCE: ContextVar[dict[str, Any] | None] = ContextVar("unimem_ingest_modality", default=None)


class HeldForReview(PermissionError):
    """A canonical write was refused; only its commitment entered the review log."""


def enabled(root: str) -> bool:
    return os.path.isfile(fp.filename(root, "subject-policy.json"))


@lru_cache(maxsize=8)
def _load(root: str, stamp: Any, pins: Any) -> dict[str, Any]:
    with open(fp.filename(root, "subject-policy.json"), "rb") as stream:
        raw = stream.read(fp.MAX_BYTES+1)
    if len(raw) > fp.MAX_BYTES:
        raise PermissionError("UniMem subject policy exceeds its bound")
    signed = json.loads(raw)
    if not fp.verify(root, signed) or signed["record"].get("schema") != "unimem.subject-policy.v1":
        raise PermissionError("UniMem subject policy signature is invalid")
    return dict(signed["record"])


def _policy(root: str) -> dict[str, Any]:
    return _load(root, fp._stamp(fp.filename(root, "subject-policy.json")), fp._pins(root))


def _labels(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 64 or len(set(value)) != len(value):
        raise ValueError("UniMem subject policy labels require a distinct bounded list")
    return sorted(fp.text(item, "label", 256) for item in value)


def compile(root: str, bindings: list[dict[str, Any]]) -> dict[str, Any]:
    from commontrace import incident, memory_authority, nextwave_store, transparency_log

    nextwave_store.operator()
    fp.count(len(bindings), 1, 64)
    cleaned = []
    for item in bindings:
        if set(item) - {"id", "scopes", "subject_class", "context", "retention_days", "retention_purpose",
                        "parental_consent"} or not {"id", "scopes", "subject_class", "context"} <= set(item):
            raise ValueError("UniMem subject policy binding has unknown or missing fields")
        row = {"id": fp.text(item["id"], "rule binding", 128), "scopes": _labels(item["scopes"]),
               "subject_class": item["subject_class"], "context": item["context"]}
        if row["subject_class"] not in CLASSES or row["context"] not in CONTEXTS:
            raise ValueError("UniMem unsupported subject class or deployment context")
        if row["subject_class"] == "child-under-13":
            row["retention_days"] = fp.count(item.get("retention_days", 0), 1, 365, "written child retention days")
            row["retention_purpose"] = fp.text(item.get("retention_purpose", ""), "written retention purpose", 256)
            consent = item.get("parental_consent")
            if consent is not None:
                if not isinstance(consent, dict) or set(consent) != {"reference", "expires_at", "verification_method"}:
                    raise ValueError("UniMem parental consent needs a reference, expiry and verification method")
                row["parental_consent"] = {"reference": fp.text(consent["reference"], "consent reference", 256),
                    "expires_at": fp.moment(consent["expires_at"]),
                    "verification_method": fp.text(consent["verification_method"], "parental verification", 256)}
            else:
                row["parental_consent"] = None  # Compiling does not fabricate parental consent.
        cleaned.append(row)
    if len({row["id"] for row in cleaned}) != len(cleaned):
        raise ValueError("UniMem subject rule IDs must be distinct")
    body = {"schema": "unimem.subject-policy.v1", "packs": list(PACKS), "bindings": cleaned,
            "recorded_by": list(memory_authority.bound_writer()), "issued_at": fp.now(),
            "assertion": "operator enrollment; parental verification evidence remains external"}
    with incident.protect(root), _jsonl.locked(fp.filename(root, "subject-policy.json")):
        signed = fp.sign(root, body)
        _jsonl.write_json(fp.filename(root, "subject-policy.json"), signed)
        anchor = transparency_log.append(root, "subject-policy-compiled", {"policy_digest": fp.digest(signed)})
    return {"policy": signed, "anchor": anchor}


@contextmanager
def ingest(root: str, source: Mapping[str, Any] | None = None) -> Iterator[None]:
    if source is not None:
        if not isinstance(source, Mapping) or set(source) - {"modality", "labels", "inferred"}:
            raise ValueError("UniMem source tags accept modality, labels and inferred")
        modality = source.get("modality", "unknown")
        if modality not in MODALITIES or type(source.get("inferred", False)) is not bool:
            raise ValueError("UniMem invalid source modality or inference tag")
        value = {"modality": modality, "labels": _labels(source.get("labels", [])),
                 "inferred": source.get("inferred", False)}
        if not set(value["labels"]) <= EMOTIONS | {"observation", "preference", "profile", "procedure", "other"}:
            raise ValueError("UniMem semantic source tags must use the bounded profile")
    else:
        value = None
    token = _SOURCE.set(value)
    try:
        yield
    finally:
        _SOURCE.reset(token)


def _source(record: Mapping[str, Any]) -> dict[str, Any]:
    declared = _SOURCE.get()
    provenance = record.get("provenance", {})
    source = declared if declared is not None else provenance.get("source", {})
    if not isinstance(source, dict):
        return {"modality": "unknown", "labels": [], "inferred": False}
    modality = source.get("modality", "unknown")
    labels = source.get("labels", [])
    labels = set(labels) if isinstance(labels, list) and all(isinstance(v, str) for v in labels) else set()
    if not labels <= EMOTIONS | {"observation", "preference", "profile", "procedure", "other"}:
        modality = "unknown"
        labels &= EMOTIONS
    if record.get("category") in EMOTIONS:
        labels.add(record["category"])
    return {"modality": modality if modality in MODALITIES else "unknown", "labels": sorted(labels),
            "inferred": bool(source.get("inferred", False)) or bool(labels & EMOTIONS and modality in BIOMETRIC)}


def evaluate(policy: Mapping[str, Any], record: Mapping[str, Any], *, purpose: str, at: str,
             source: Mapping[str, Any]) -> dict[str, Any]:
    fp.wire(record)
    labels = set(record.get("scopes", []))
    matching = [row for row in policy["bindings"] if set(row["scopes"]) <= labels]
    result: dict[str, Any] = {"decision": "allow", "reasons": [],
                              "bindings": [fp.digest(row["id"]) for row in matching],
                              "biometric_personal_information": False, "retention_deadline": None}
    if not matching:
        result["reasons"] = ["outside-enrolled-context"]
        return result
    decisions = []
    for row in matching:
        if source["modality"] == "unknown":
            decisions.append("review")
            result["reasons"].append("unknown-source-modality")
        emotion = bool(set(source["labels"]) & EMOTIONS) and source["inferred"]
        if (row["context"] in {"workplace", "education"} and source["modality"] in BIOMETRIC and emotion
                and purpose not in {MEDICAL, SAFETY}):
            decisions.append("quarantine")
            result["reasons"].append("eu-ai-act-biometric-emotion")
        if row["subject_class"] == "child-under-13":
            result["biometric_personal_information"] |= source["modality"] in BIOMETRIC
            parent = row["parental_consent"]
            if parent is None or fp.moment(parent["expires_at"]) <= at:
                decisions.append("deny")
                result["reasons"].append("parental-consent-missing-or-expired")
            if purpose != row["retention_purpose"]:
                decisions.append("deny")
                result["reasons"].append("child-retention-purpose-mismatch")
            deadline = (datetime.fromisoformat(fp.moment(at))+timedelta(days=row["retention_days"])).isoformat()
            if parent is not None:
                deadline = min(deadline, fp.moment(parent["expires_at"]))
            result["retention_deadline"] = min(result["retention_deadline"] or deadline, deadline)
    priority = {"allow": 0, "review": 1, "quarantine": 2, "deny": 3}
    result["decision"] = max(["allow", *decisions], key=priority.__getitem__)
    result["reasons"] = sorted(set(result["reasons"])) or ["enrolled-rule-packs-allow"]
    return result


def check(root: str, record: dict[str, Any], *, source: dict[str, Any] | None = None,
          purpose: str | None = None) -> dict[str, Any]:
    """Operator-only dry run; no canonical bytes or decision event are written."""
    from commontrace import nextwave_store
    from commontrace import purpose as purpose_io

    nextwave_store.operator()
    with ingest(root, source), purpose_io.request(root, purpose):
        return evaluate(_policy(root), record, purpose=purpose_io.current(root), at=fp.now(), source=_source(record))


def assert_write(root: str, record: Mapping[str, Any]) -> None:
    if not enabled(root):
        return
    from commontrace import memory_authority, purpose, transparency_log

    policy = _policy(root)
    tags = _source(record)
    decision = evaluate(policy, record, purpose=purpose.current(root), at=fp.now(), source=tags)
    if not permits(root, record):
        decision["decision"] = "deny"
        decision["reasons"] = sorted(set(decision["reasons"]) | {"retention-deadline-ended"})
    commitment = {"record_digest": fp.digest(record), "policy_digest": fp.digest(policy),
                  "source_digest": fp.digest(tags), "purpose_digest": fp.digest(purpose.current(root)),
                  "actor_digest": fp.digest(memory_authority.bound_writer()), "issued_at": fp.now(), **decision}
    signed = fp.append(root, "subject-decisions.jsonl", commitment)
    transparency_log.append(root, "subject-write-decision", {"decision_digest": fp.digest(signed)})
    if decision["decision"] != "allow":
        raise HeldForReview("UniMem subject policy "+decision["decision"]+": "+",".join(decision["reasons"])+
                            "; decision "+fp.digest(signed))
    from commontrace import nextwave_store

    identity = _identity(root, record)
    if identity and decision["bindings"]:
        prior = nextwave_store.latest(root, "subject-sources")
        if identity not in prior:
            fp.count(len(prior), 0, nextwave_store.MAX_OBJECTS-1, "regulated source tags")
        nextwave_store.event(root, "subject-sources", identity, {"source": tags,
            "record_digest": commitment["record_digest"], "policy_digest": commitment["policy_digest"]})
    if decision["retention_deadline"] is not None:
        _schedule(root, record, decision["retention_deadline"])


def _identity(root: str, record: Mapping[str, Any]) -> str:
    from commontrace import nextwave_store, purpose

    if "data" in record and "revision" in record:
        return str(record["id"])
    if record.get("kind") and not (record.get("kind") in NATIVE_KINDS and "payload" in record):
        return nextwave_store.reference(record["kind"], record["id"])
    return purpose._key(root, record)


def _schedule(root: str, record: Mapping[str, Any], deadline: str) -> None:
    with _jsonl.locked(fp.filename(root, RETENTION)):
        _schedule_locked(root, record, deadline)


def _schedule_locked(root: str, record: Mapping[str, Any], deadline: str) -> None:
    from commontrace import memory_authority, nextwave_store

    identity = _identity(root, record)
    if not identity:
        raise ValueError("UniMem regulated memory requires a stable identity")
    previous = _retentions(root).get(identity)
    if previous:
        deadline = min(deadline, previous["expires_at"])
    if memory_authority.record_class(dict(record)).startswith("control:"):
        namespace = "controls"
    elif record.get("kind") and not (record.get("kind") in NATIVE_KINDS and "payload" in record):
        namespace = "artifacts"
    elif ("payload" in record and record.get("kind") in NATIVE_KINDS) or record.get("memory_type") == "capture":
        namespace = "records"
    else:
        namespace = {"fact": "facts", "trace": "traces"}.get(memory_authority.record_class(dict(record)), "lessons")
    value = {"expires_at": deadline, "namespace": namespace, "scopes": record.get("scopes", []),
             "native_id": record["id"] if namespace == "records" else None}
    if previous is None or any(previous.get(key) != item for key, item in value.items()):
        fp.count(len(_retentions(root))+(previous is None), 1, nextwave_store.MAX_OBJECTS, "retention schedules")
        fp.append(root, RETENTION, {"id": identity, **value})


@lru_cache(maxsize=8)
def _retention_snapshot(root: str, count: int, tip: str) -> dict[str, dict[str, Any]]:
    result = {row["id"]: row for row in fp.journal(root, RETENTION)}
    fp.count(len(result), 0, 4096, "retention schedules")
    return result


def _retentions(root: str) -> dict[str, dict[str, Any]]:
    count, tip = fp.checkpoint(root, RETENTION)
    return _retention_snapshot(root, count, tip)


def permits(root: str, record: Mapping[str, Any]) -> bool:
    # Governance schedules outlive content erasure and policy reconfiguration.
    if not os.path.isfile(fp.filename(root, RETENTION)):
        return True
    row = _retentions(root).get(_identity(root, record))
    return row is None or fp.now() < fp.moment(row["expires_at"])


def explain(root: str, decision_id: str | None = None) -> dict[str, Any]:
    from commontrace import nextwave_store

    nextwave_store.operator()
    rows = fp.journal(root, "subject-decisions.jsonl")
    if decision_id:
        rows = [row for index, row in enumerate(rows)
                if fp.digest(fp.signed_at(root, "subject-decisions.jsonl", index)) == decision_id]
    return {"decisions": rows[-64:], "evidence": "hash-only write decisions; not a legal certification"}


def reap(root: str) -> list[dict[str, Any]]:
    from commontrace import incident, nextwave_store

    nextwave_store.operator()
    with incident.protect(root):
        return _reap(root)


def _reap(root: str) -> list[dict[str, Any]]:
    from commontrace import retention

    results = []
    for identity, row in _retentions(root).items():
        if fp.moment(row["expires_at"]) <= fp.now():
            receipt = {"id": "regulated-"+fp.digest(identity), "issued_at": row["recorded_at"],
                "expires_at": row["expires_at"], "status": "expired", "vault_directory": None,
                "sources": {identity: {"namespace": row["namespace"], "scopes": row["scopes"],
                                       "native_id": row["native_id"]}}}
            results.append(retention.expire_consent(root, receipt))
    return results
