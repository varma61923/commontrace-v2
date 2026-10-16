"""Incident snapshots, legal holds and offline-verifiable evidence packs.

Packs authenticate bytes and map operator evidence to control identifiers.
They are not legal advice, a finding of compliance, or proof of facts outside
the logged system. Clause mappings must be verified against the Official Journal.
Snapshots retain both valid and record time and the actual retrieval receipts;
a later correction cannot silently rewrite an earlier decision's pinned bytes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator, cast

from commontrace import _jsonl, frontmatter, hierarchical, lesson_io, lineage, memory_authority, paths, receipts
from commontrace import frontier_protocol as fp
from commontrace import transparency_log as log

JOURNAL = "incidents.jsonl"
CONTROLS = {
    "eu-ai-act": ["Art.12", "Art.19", "Art.26(6)", "Art.72", "Art.73", "Annex.IV"],
    "iso-42001": ["A.6.2.6", "A.8.2", "A.8.3", "A.10.3"],
    "nist-ai-rmf": ["GOVERN.1.5", "MEASURE.2.5", "MANAGE.2.4"],
    "nist-ai-600-1": ["confabulation", "information-integrity", "data-privacy"],
    "decision-rights": ["GDPR.Art.22", "CCPA.ADMT"],
    "product-liability": ["EU.PLD.Art.9", "EU.PLD.Art.10"],
}


MAX_PART_BYTES = 32 * fp.MAX_BYTES
MAX_SNAPSHOT_BYTES = 64 * fp.MAX_BYTES


def _operator() -> None:
    if memory_authority.WRITER.get()[1] != "operator":
        raise PermissionError("incident evidence and legal holds require an operator")


def _identity(identity: str) -> str:
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
        raise ValueError("incident id must be 1..128 letters, digits, hyphens or underscores")
    return identity


def _path(root: str, identity: str) -> str:
    """Legacy single-file snapshot location (read-only compatibility)."""
    path = os.path.join(paths.memory_dir(root), "incidents", _identity(identity) + ".json")
    paths.enforce_boundary(root, path)
    if os.path.islink(path):
        raise ValueError("incident snapshot must not be a symlink")
    return path


def _directory(root: str, identity: str) -> str:
    path = os.path.join(paths.memory_dir(root), "incidents", _identity(identity))
    paths.enforce_boundary(root, path)
    if os.path.islink(path):
        raise ValueError("incident snapshot must not be a symlink")
    return path


def _canonical(value: Any, limit: int = MAX_PART_BYTES) -> bytes:
    """Canonical JSON bytes without the 1 MiB record bound, under an explicit part bound."""
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                             allow_nan=False).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError("incident evidence must be finite JSON data") from exc
    if len(encoded) > limit:
        raise ValueError(f"incident evidence part exceeds {limit // fp.MAX_BYTES} MiB; narrow the store")
    return encoded


def large_digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value, MAX_SNAPSHOT_BYTES)).hexdigest()


def _read_part(directory: str, name: str, expected: str) -> Any:
    path = os.path.join(directory, name)
    if os.path.islink(path):
        raise ValueError("incident evidence part must not be a symlink")
    with open(path, "rb") as stream:
        data = stream.read(MAX_PART_BYTES + 1)
    if len(data) > MAX_PART_BYTES or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"incident evidence part {name} does not match its signed digest")
    return json.loads(data)


def snapshot(root: str, identity: str) -> dict[str, Any]:
    """The frozen evidence, every part verified against the signed manifest."""
    directory = _directory(root, identity)
    manifest_path = os.path.join(directory, "manifest.json")
    if not os.path.exists(manifest_path):
        with open(_path(root, identity), "rb") as stream:
            data = stream.read(fp.MAX_BYTES + 1)
        if len(data) > fp.MAX_BYTES:
            raise ValueError("incident snapshot exceeds 1 MiB")
        legacy = json.loads(data)
        if not fp.verify(root, legacy):
            raise ValueError("incident snapshot signature is invalid")
        record: dict[str, Any] = legacy["record"]
        return record
    with open(manifest_path, "rb") as stream:
        data = stream.read(fp.MAX_BYTES + 1)
    if len(data) > fp.MAX_BYTES:
        raise ValueError("incident manifest exceeds 1 MiB")
    signed = json.loads(data)
    if not fp.verify(root, signed):
        raise ValueError("incident snapshot signature is invalid")
    manifest: dict[str, Any] = signed["record"]
    result = {key: value for key, value in manifest.items() if key != "parts"}
    for name, expected in manifest["parts"].items():
        result[name.removesuffix(".json")] = _read_part(directory, name, expected)
    return result


def _lessons(root: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    directory = Path(paths.lessons_dir(root))
    for filename in sorted(directory.glob("*.md")):
        paths.enforce_boundary(root, str(filename))
        if filename.stat().st_size > MAX_PART_BYTES:
            raise ValueError("snapshot lesson exceeds the evidence part bound")
        fm, body = frontmatter.read(str(filename))
        records.append({"id": lesson_io.canonical_slug(filename.name), "frontmatter": fm,
                        "body": body, "revision": lesson_io.current_revision(str(filename))})
    return records


def freeze(root: str, incident_id: str, valid_time: str) -> dict[str, Any]:
    """Pin the bitemporal store state at ``valid_time`` under a legal hold.

    Facts are captured twice: as believed at the incident (record time =
    valid time) and as known at the freeze (later corrections included), so
    the timeline can show what was believed then versus corrected since.
    Evidence is written as content-addressed parts under one signed manifest;
    only the manifest digest enters the incident journal and transparency log.
    """
    _operator()
    valid_time = fp.moment(valid_time)
    directory = _directory(root, incident_id)
    with _jsonl.locked(fp.filename(root, JOURNAL)):
        if os.path.exists(directory) or os.path.exists(_path(root, incident_id)):
            raise ValueError("incident id already frozen; use a new incident id")
        moment = fp.now()
        believed = [f.to_dict() for f in hierarchical.list_facts(root, status="", as_of=valid_time,
                                                                 known_at=min(valid_time, moment))]
        known = [f.to_dict() for f in hierarchical.list_facts(root, status="", as_of=valid_time, known_at=moment)]
        lesson_records = _lessons(root)
        history, corrupt = lesson_io.read_revisions(root)
        if corrupt:
            raise ValueError("cannot freeze corrupt lesson revision history")
        retrieved = [r.to_dict() for r in receipts.read_all(root) if fp.moment(r.at) <= valid_time]
        identities = sorted({str(f["id"]) for f in [*believed, *known]}
                            | {str(lesson["id"]) for lesson in lesson_records})
        from commontrace import allocation, evidence, holdout_io, integrity, retrieval_io

        analysis = evidence.analyse(root)
        parts: dict[str, Any] = {
            "facts.json": known, "facts_believed.json": believed, "lessons.json": lesson_records,
            "lesson_history.json": history, "retrievals.json": retrieved, "held_ids.json": identities,
            "lineage.json": {identity: lineage.explain(root, identity) for identity in identities},
            "technical_documentation.json": {
                "retrieval": asdict(retrieval_io.load_config(root)),
                "experiment": asdict(holdout_io.load_config(root)),
                "schedules": [row.body() for row in allocation.history(root)]},
            "post_market_monitoring.json": {
                "effects": [asdict(effect) for effect in analysis.effects],
                "integrity": json.loads(json.dumps(asdict(cast(integrity.IntegrityReport, analysis.report)),
                                                   default=str)) if analysis.report else None},
        }
        encoded = {name: _canonical(value) for name, value in parts.items()}
        if sum(map(len, encoded.values())) > MAX_SNAPSHOT_BYTES:
            raise ValueError("incident evidence exceeds 64 MiB; freeze a narrower store")
        part_digests = {name: hashlib.sha256(data).hexdigest() for name, data in encoded.items()}
        manifest = {"id": incident_id, "valid_time": valid_time, "known_at": moment, "parts": part_digests,
                    "held_count": len(identities), "control_mapping": CONTROLS,
                    "legal_note": "Verify clause mappings and applicability against the Official Journal."}
        signed = fp.sign(root, manifest)
        staging = directory + ".partial"
        if os.path.lexists(staging):
            import shutil

            shutil.rmtree(staging)
        os.makedirs(staging)
        for name, data in encoded.items():
            with open(os.path.join(staging, name), "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        _jsonl.write_json(os.path.join(staging, "manifest.json"), signed)
        os.replace(staging, directory)
        digest = fp.digest(signed)
        fp.append(root, JOURNAL, {"kind": "freeze", "id": incident_id, "held_count": len(identities),
                                  "held_ids_digest": part_digests["held_ids.json"],
                                  "snapshot_digest": digest, "valid_time": valid_time})
        anchor = log.append(root, "incident-freeze", {"id": incident_id, "snapshot_digest": digest})
        return {"id": incident_id, "held_ids": identities, "anchor": anchor}


def release(root: str, identity: str, reason: str) -> dict[str, Any]:
    _operator()
    snapshot(root, identity)
    with _jsonl.locked(fp.filename(root, JOURNAL)):
        signed = fp.append(root, JOURNAL, {"kind": "release", "id": identity, "reason": fp.text(reason)})
        log.append(root, "incident-hold-release", {"id": identity, "reason": reason})
        return signed


_HELD: dict[str, tuple[tuple[int, str], dict[str, list[str]]]] = {}


def held(root: str) -> dict[str, list[str]]:
    """Active legal holds: incident id -> pinned memory ids (verified against the manifest)."""
    key = os.path.realpath(root)
    checkpoint = fp.checkpoint(root, JOURNAL)
    cached = _HELD.get(key)
    if cached is not None and cached[0] == checkpoint:
        return {incident: list(ids) for incident, ids in cached[1].items()}
    active: dict[str, list[str]] = {}
    for event in fp.journal(root, JOURNAL):
        if event["kind"] == "freeze":
            if "held_ids" in event:  # Legacy journal rows carried the ids inline.
                active[event["id"]] = event["held_ids"]
            else:
                active[event["id"]] = _read_part(_directory(root, event["id"]), "held_ids.json",
                                                 event["held_ids_digest"])
        elif event["kind"] == "release":
            active.pop(event["id"], None)
    if len(_HELD) > 64:
        _HELD.clear()
    _HELD[key] = (checkpoint, active)
    return {incident: list(ids) for incident, ids in active.items()}


def assert_unpinned(root: str, identities: list[str]) -> None:
    for incident_id, held_ids in held(root).items():
        if set(identities) & set(held_ids):
            raise PermissionError(f"legal hold for incident {incident_id}; explicitly release the hold before erasure")


@contextmanager
def protect(root: str) -> Iterator[None]:
    with _jsonl.locked(fp.filename(root, JOURNAL)):
        yield


def timeline(root: str, identity: str) -> dict[str, Any]:
    """What was retrieved, believed then, known at the freeze, and corrected since."""
    frozen = snapshot(root, identity)
    live = hierarchical.load_facts(root)
    known = {fact["id"]: fact for fact in frozen["facts"]}
    believed = frozen.get("facts_believed", frozen["facts"])
    corrections = []
    for fact in {row["id"]: row for row in [*believed, *frozen["facts"]]}.values():
        current = live.get(fact["id"])
        corrections.append({
            "id": fact["id"], "then": fact["statement"],
            "believed_at_incident": any(row["id"] == fact["id"] for row in believed),
            "known_at_freeze": known.get(fact["id"], {}).get("status"),
            "current_status": current.status if current else "erased",
            "superseded_by": current.superseded_by if current else None})
    approvals = {lesson["id"]: lesson["frontmatter"].get("approval_receipt") is not None
                 for lesson in frozen["lessons"]}
    return {"id": identity, "valid_time": frozen["valid_time"], "known_at": frozen["known_at"],
            "retrievals": frozen["retrievals"], "lineage": frozen["lineage"], "corrections": corrections,
            "approved_lessons": approvals, "held": identity in held(root)}


def replay(root: str, identity: str, occasion_id: str) -> dict[str, Any]:
    """Reconstruct delivered lesson bytes from the frozen content revision history."""
    frozen = snapshot(root, identity)
    decisions = [r for r in frozen["retrievals"] if r["occasion_id"] == occasion_id]
    if len(decisions) != 1:
        raise ValueError("replay needs exactly one recorded retrieval occasion")
    revisions = {row["revision"]: {"body": row["body"], "frontmatter": row["frontmatter"]}
                 for row in frozen["lessons"]}
    for event in frozen["lesson_history"]:
        if "before_body" in event:
            revisions[event["from"]] = {"body": event["before_body"], "frontmatter": event["before_frontmatter"]}
    delivered = []
    for row in decisions[0]["admitted"]:
        if row["revision"] not in revisions:
            raise ValueError("historical lesson bytes are unavailable; cannot claim an exact replay")
        delivered.append({**row, **revisions[row["revision"]]})
    return {"occasion_id": occasion_id, "injected": delivered, "withheld": decisions[0]["withheld"]}


def pack(root: str, identity: str, *, framework: str = "eu-ai-act") -> dict[str, Any]:
    _operator()
    if framework not in CONTROLS:
        raise ValueError("unknown evidence framework")
    record = snapshot(root, identity)
    document = {"snapshot": record, "timeline": timeline(root, identity), "framework": framework,
                "control_ids": CONTROLS[framework], "attestation": "Evidence mapping; not a compliance certification",
                "legal_note": "Verify against the Official Journal and applicable published standards."}
    manifest = {"id": identity, "framework": framework, "files": {"evidence.json": large_digest(document)},
                "held_count": len(record["held_ids"]), "issued_at": fp.now()}
    signed = fp.sign(root, manifest)
    anchor = log.append(root, "incident-evidence-pack", {"manifest_digest": fp.digest(signed)})
    return {"manifest": signed, "files": {"evidence.json": document}, "anchor": anchor}


def verify_pack(root: str, bundle: dict[str, Any]) -> bool:
    try:
        signed = bundle["manifest"]
        return (fp.verify(root, signed) and log.verify(root, bundle["anchor"])
                and bundle["anchor"]["statement"]["record"]["statement"]["manifest_digest"] == fp.digest(signed)
                and set(signed["record"]["files"]) == set(bundle["files"])
                and all(large_digest(bundle["files"][name]) == digest
                        for name, digest in signed["record"]["files"].items()))
    except (ValueError, KeyError, TypeError):
        return False
