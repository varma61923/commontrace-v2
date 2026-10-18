"""Content-addressed bounded memory passports, aligned with PAM field names.

The UniMem format uses SHA-256 rather than PAM's BLAKE3; it does not assert PAM
wire compatibility. A signature authenticates provenance claims, not their truth.
Imports preserve the exact original payload and enter review before admission.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from commontrace import frontier_protocol as fp
from commontrace import transparency_log as merkle

COMPONENTS = ("episodic", "semantic", "procedural", "working", "identity")
ENTRY_FIELDS = {"parent_ids", "created_at", "version", "content", "provenance", "confidence", "timestamps",
                "consent_scope", "labels", "capability_tokens"}
MAX_ENTRIES = 64
MAX_BYTES = 256*1024


def model_digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        raise ValueError("UniMem producing model needs an explicit SHA-256 digest")
    return value


def _hash(value: Any) -> str:
    return "sha256:"+fp.digest(value)


def entry(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != ENTRY_FIELDS:
        raise ValueError("UniMem passport entry must include content and all provenance/consent fields")
    clean = dict(json.loads(fp.wire(value)))
    fp.count(len(fp.wire(clean["content"])), 1, 32768, "passport source bytes")
    if not isinstance(clean["content"], dict) or not isinstance(clean["provenance"], dict):
        raise ValueError("UniMem passport content and provenance must be objects")
    source = clean["provenance"]
    if not {"source_id", "namespace"} <= set(source):
        raise ValueError("UniMem passport entry requires its canonical source namespace and id")
    fp.text(source["source_id"], "passport source id", 256)
    if source["namespace"] not in {"facts", "lessons", "traces", "records"}:
        raise ValueError("UniMem unsupported canonical passport namespace")
    fp.moment(clean["created_at"])
    fp.text(clean["version"], "passport entry version", 128)
    parents = clean["parent_ids"]
    if not isinstance(parents, list) or len(parents) > 64 or len(set(parents)) != len(parents):
        raise ValueError("UniMem passport parents must be a bounded unique list")
    for parent in parents:
        fp.text(parent, "passport parent", 256)
    labels = clean["labels"]
    if not isinstance(labels, list) or len(labels) > 128 or len(set(labels)) != len(labels):
        raise ValueError("UniMem passport labels must be a bounded unique list")
    for label in labels:
        fp.text(label, "passport label", 256)
    if clean["confidence"] is not None:
        fp.number(clean["confidence"], 0, 1)
    if not isinstance(clean["timestamps"], dict) or len(clean["timestamps"]) > 8:
        raise ValueError("UniMem passport timestamps must be a bounded object")
    for name, timestamp in clean["timestamps"].items():
        fp.text(name, "passport timestamp field", 64)
        if timestamp is not None:
            fp.moment(timestamp)
    if not isinstance(clean["consent_scope"], dict) or len(fp.wire(clean["consent_scope"])) > 8192:
        raise ValueError("UniMem passport consent scope must be a bounded explicit object")
    grants = clean["capability_tokens"]
    if not isinstance(grants, list) or len(grants) > 8 or any(not isinstance(grant, dict) for grant in grants):
        raise ValueError("UniMem passport capability grants must be explicit bounded envelopes")
    # Preserve every canonical field and order within source snapshots.
    return {"id": _hash(clean), **clean}


def build(components: Mapping[str, Sequence[Mapping[str, Any]]], *, created_at: str,
          source_agent: Mapping[str, Any]) -> dict[str, Any]:
    fp.moment(created_at)
    if set(components)-set(COMPONENTS) or not isinstance(source_agent, dict):
        raise ValueError("UniMem passport requires known memory components and source-agent metadata")
    if set(source_agent) != {"name", "model_digest", "model_identity", "runtime"}:
        raise ValueError("UniMem passport needs explicit source-agent and model identity metadata")
    fp.text(source_agent["name"], "source agent", 128)
    model_digest(source_agent["model_digest"])
    if source_agent["model_identity"] != "operator-declared digest; execution not attested":
        raise ValueError("UniMem passport cannot claim unverified model execution")
    if source_agent["runtime"] != "unimem":
        raise ValueError("UniMem unknown passport runtime")
    count = sum(len(values) for values in components.values())
    fp.count(count, 1, MAX_ENTRIES)
    groups = {kind: sorted([entry(value) for value in components.get(kind, [])], key=lambda value: value["id"])
              for kind in COMPONENTS}
    entries = [value for kind in COMPONENTS for value in groups[kind]]
    identities = [(value["provenance"]["namespace"], value["provenance"]["source_id"]) for value in entries]
    if len(set(identities)) != len(identities):
        raise ValueError("UniMem a passport cannot contain repeated canonical source identities")
    value = {"format": "unimem-passport", "schema_version": "1.0", "created_at": created_at,
             "source_agent": dict(source_agent), "components": groups, "entry_count": count,
             "hash_algorithm": "sha256",
             "root_hash": "sha256:"+merkle.tree_hash([merkle.leaf_hash(e) for e in entries]),
             "mapping": "PAM 1.0 field alignment; UniMem SHA-256 format, not PAM wire compatibility"}
    if len(fp.wire(value)) > MAX_BYTES:
        raise ValueError("UniMem passport exceeds 256 KiB; export smaller scoped segments")
    return dict(json.loads(fp.wire(value)))


def verify(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if len(fp.wire(manifest)) > MAX_BYTES:
        raise ValueError("UniMem passport exceeds 256 KiB")
    if not isinstance(manifest.get("components"), dict) or set(manifest["components"]) != set(COMPONENTS):
        raise ValueError("UniMem passport must declare the five memory components")
    components: dict[str, list[dict[str, Any]]] = {}
    for kind in COMPONENTS:
        values = manifest["components"][kind]
        if not isinstance(values, list) or len(values) > MAX_ENTRIES:
            raise ValueError("UniMem passport component exceeds its bound")
        components[kind] = []
        for value in values:
            if not isinstance(value, dict) or set(value) != ENTRY_FIELDS | {"id"}:
                raise ValueError("UniMem malformed content-addressed passport entry")
            content = {name: field for name, field in value.items() if name != "id"}
            if entry(content) != value:
                raise PermissionError("UniMem passport entry content differs from its address")
            components[kind].append(content)
    expected = build(components, created_at=manifest["created_at"], source_agent=manifest["source_agent"])
    if expected != manifest:
        raise PermissionError("UniMem passport Merkle root, schema or manifest commitment does not match")
    return expected


def stage(text: str, *, source: str) -> list[dict[str, Any]]:
    """Screen inbound assistant text, retaining review status even for clean input."""
    from commontrace import defense, injection_guard, memory_guard

    fp.text(source, "self-reported import source", 128)
    fp.text(text, "inbound memory summary", 128*1024)
    source_decision = defense.screen_content(source)
    if source_decision.action is defense.DefenseAction.BLOCK:
        source = "[blocked source label]"
    elif source_decision.redacted_content is not None:
        source = source_decision.redacted_content
    source, _ = memory_guard.redact_pii(source)
    pieces: list[str] = []
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, dict) and set(value) == {"memories"} and isinstance(value["memories"], list):
        if any(not isinstance(item, str) for item in value["memories"]):
            raise ValueError("UniMem inbound summary memories must be strings")
        pieces = value["memories"]
    elif isinstance(value, list):
        if any(not isinstance(item, str) for item in value):
            raise ValueError("UniMem inbound summary memories must be strings")
        pieces = value
    else:
        pieces = [piece.strip() for piece in re.split(r"\n\s*\n", text) if piece.strip()]
    fp.count(len(pieces), 1, 32, "inbound candidate memories")
    result = []
    for piece in pieces:
        fp.text(piece, "inbound candidate", 32768)
        labels = injection_guard.injection_labels({"text": source+"\n"+piece})
        decision = defense.screen_content(piece)
        safe = decision.redacted_content if decision.redacted_content is not None else piece
        if decision.action is defense.DefenseAction.BLOCK:
            safe = "[blocked by the memory defense screen]"
        safe, pii = memory_guard.redact_pii(safe)
        result.append({"text": safe, "source": source, "status": "review", "verified": False,
            "provenance": "unverified, self-reported via "+source, "raw_digest": fp.digest(piece),
            "screening": {"injection_labels": labels, "defense_action": decision.action.value,
                          "matched_types": sorted(set(decision.matched_types) | set(pii))},
            "classification": "unverified candidate; never automatically admitted"})
    return result
