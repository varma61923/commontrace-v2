"""Incident snapshots, legal holds and offline-verifiable evidence packs.

Packs authenticate bytes and map operator evidence to control identifiers.
They are not legal advice, a finding of compliance, or proof of facts outside
the logged system. Clause mappings must be verified against the Official Journal.
Snapshots retain both valid and record time and the actual retrieval receipts;
a later correction cannot silently rewrite an earlier decision's pinned bytes.
"""
from __future__ import annotations

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


def _operator() -> None:
    if memory_authority.WRITER.get()[1] != "operator":
        raise PermissionError("incident evidence and legal holds require an operator")


def _path(root: str, identity: str) -> str:
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
        raise ValueError("incident id must be 1..128 letters, digits, hyphens or underscores")
    path = os.path.join(paths.memory_dir(root), "incidents", identity + ".json")
    paths.enforce_boundary(root, path)
    if os.path.islink(path):
        raise ValueError("incident snapshot must not be a symlink")
    return path


def snapshot(root: str, identity: str) -> dict[str, Any]:
    with open(_path(root, identity), "rb") as stream:
        data = stream.read(fp.MAX_BYTES + 1)
    if len(data) > fp.MAX_BYTES:
        raise ValueError("incident snapshot exceeds 1 MiB")
    signed = json.loads(data)
    if not fp.verify(root, signed):
        raise ValueError("incident snapshot signature is invalid")
    record: dict[str, Any] = signed["record"]
    return record


def freeze(root: str, incident_id: str, valid_time: str) -> dict[str, Any]:
    _operator()
    valid_time = fp.moment(valid_time)
    path = _path(root, incident_id)
    with _jsonl.locked(fp.filename(root, JOURNAL)):
        if os.path.exists(path):
            raise ValueError("incident id already frozen; use a new incident id")
        moment = fp.now()
        facts = [f.to_dict() for f in hierarchical.list_facts(root, status="", as_of=valid_time, known_at=moment)]
        lesson_records: list[dict[str, Any]] = []
        directory = Path(paths.lessons_dir(root))
        for i, filename in enumerate(sorted(directory.glob("*.md"))):
            fp.count(i, 0, 999, "snapshot lesson count")
            paths.enforce_boundary(root, str(filename))
            if filename.stat().st_size > 131_072:
                raise ValueError("snapshot lesson exceeds 128 KiB")
            fm, body = frontmatter.read(str(filename))
            lesson_records.append({"id": lesson_io.canonical_slug(filename.name), "frontmatter": fm,
                                   "body": body, "revision": lesson_io.current_revision(str(filename))})
        history, corrupt = lesson_io.read_revisions(root)
        if corrupt:
            raise ValueError("cannot freeze corrupt lesson revision history")
        retrieved = [r.to_dict() for r in receipts.read_all(root) if fp.moment(r.at) <= valid_time]
        identities = sorted({str(f["id"]) for f in facts} | {str(lesson["id"]) for lesson in lesson_records})
        from commontrace import allocation, evidence, holdout_io, integrity, retrieval_io

        analysis = evidence.analyse(root)
        payload: dict[str, Any] = {"id": incident_id, "valid_time": valid_time, "known_at": moment, "facts": facts,
                "lessons": lesson_records, "lesson_history": history, "retrievals": retrieved,
                "lineage": {identity: lineage.explain(root, identity) for identity in identities},
                "held_ids": identities, "control_mapping": CONTROLS,
                "technical_documentation": {"retrieval": asdict(retrieval_io.load_config(root)),
                                             "experiment": asdict(holdout_io.load_config(root)),
                                             "schedules": [row.body() for row in allocation.history(root)]},
                "post_market_monitoring": {"effects": [asdict(effect) for effect in analysis.effects],
                                           "integrity": json.loads(json.dumps(
                                               asdict(cast(integrity.IntegrityReport, analysis.report)), default=str))
                                           if analysis.report else None},
                "legal_note": "Verify clause mappings and applicability against the Official Journal."}
        signed = fp.sign(root, payload)
        fp.wire(signed)  # Bound the entire snapshot before publishing a hold.
        _jsonl.write_json(path, signed)
        fp.append(root, JOURNAL, {"kind": "freeze", "id": incident_id, "held_ids": identities,
                                  "snapshot_digest": fp.digest(signed), "valid_time": valid_time})
        anchor = log.append(root, "incident-freeze", {"id": incident_id, "snapshot_digest": fp.digest(signed)})
        return {"id": incident_id, "held_ids": identities, "anchor": anchor}


def release(root: str, identity: str, reason: str) -> dict[str, Any]:
    _operator()
    snapshot(root, identity)
    with _jsonl.locked(fp.filename(root, JOURNAL)):
        signed = fp.append(root, JOURNAL, {"kind": "release", "id": identity, "reason": fp.text(reason)})
        log.append(root, "incident-hold-release", {"id": identity, "reason": reason})
        return signed


def held(root: str) -> dict[str, list[str]]:
    active: dict[str, list[str]] = {}
    for event in fp.journal(root, JOURNAL):
        if event["kind"] == "freeze":
            active[event["id"]] = event["held_ids"]
        elif event["kind"] == "release":
            active.pop(event["id"], None)
    return active


def assert_unpinned(root: str, identities: list[str]) -> None:
    for incident_id, held_ids in held(root).items():
        if set(identities) & set(held_ids):
            raise PermissionError(f"legal hold for incident {incident_id}; explicitly release the hold before erasure")


@contextmanager
def protect(root: str) -> Iterator[None]:
    with _jsonl.locked(fp.filename(root, JOURNAL)):
        yield


def timeline(root: str, identity: str) -> dict[str, Any]:
    frozen = snapshot(root, identity)
    live = hierarchical.load_facts(root)
    corrections = [{"id": fact["id"], "then": fact["statement"],
                    "current_status": live[fact["id"]].status if fact["id"] in live else "erased",
                    "superseded_by": live[fact["id"]].superseded_by if fact["id"] in live else None}
                   for fact in frozen["facts"]]
    return {"id": identity, "valid_time": frozen["valid_time"], "known_at": frozen["known_at"],
            "retrievals": frozen["retrievals"], "lineage": frozen["lineage"], "corrections": corrections,
            "held": identity in held(root)}


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
    manifest = {"id": identity, "framework": framework, "files": {"evidence.json": fp.digest(document)},
                "held_ids": record["held_ids"], "issued_at": fp.now()}
    signed = fp.sign(root, manifest)
    anchor = log.append(root, "incident-evidence-pack", {"manifest_digest": fp.digest(signed)})
    return {"manifest": signed, "files": {"evidence.json": document}, "anchor": anchor}


def verify_pack(root: str, bundle: dict[str, Any]) -> bool:
    try:
        signed = bundle["manifest"]
        return (fp.verify(root, signed) and log.verify(root, bundle["anchor"])
                and bundle["anchor"]["statement"]["record"]["statement"]["manifest_digest"] == fp.digest(signed)
                and set(signed["record"]["files"]) == set(bundle["files"])
                and all(fp.digest(bundle["files"][name]) == digest
                        for name, digest in signed["record"]["files"].items()))
    except (ValueError, KeyError, TypeError):
        return False
