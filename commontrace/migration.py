"""Bounded, replayable vendor imports with source-bound external authority."""
from __future__ import annotations

import hashlib
import json
import os
import uuid

from commontrace import _jsonl, hierarchical, interop, memory_authority, paths, trace_io

VENDORS = ("mem0", "letta", "zep", "graphiti")


def graphiti_records(document) -> list[dict]:
    if not isinstance(document, dict):
        raise ValueError("Graphiti export must be an object with nodes, edges and/or episodes")
    result = []
    for item in document.get("nodes", []):
        if not isinstance(item, dict) or not item.get("uuid"):
            raise ValueError("Graphiti nodes need UUIDs")
        result.append(interop.make_record(interop.KIND_GRAPH_NODE, {
            "id": "graphiti:"+str(item["uuid"]), "entity_type": "concept", "name": str(item.get("name", "")),
            "properties": {"labels": item.get("labels", []), "summary": item.get("summary", ""),
                           "created_at": item.get("created_at"), "group_id": item.get("group_id")}}))
    for item in document.get("edges", []):
        if not isinstance(item, dict) or not item.get("source_node_uuid") or not item.get("target_node_uuid"):
            raise ValueError("Graphiti edges need source and target UUIDs")
        result.append(interop.make_record(interop.KIND_GRAPH_EDGE, {
            "source": "graphiti:"+str(item["source_node_uuid"]),
            "target": "graphiti:"+str(item["target_node_uuid"]), "relation": "relates_to",
            "valid_at": item.get("valid_at"), "invalid_at": item.get("invalid_at"),
            "expired_at": item.get("expired_at"), "properties": {"vendor_relation": item.get("name"),
                     "fact": item.get("fact"), "uuid": item.get("uuid"), "episodes": item.get("episodes", [])}}))
        if item.get("fact"):
            result.append(interop.make_record(interop.KIND_FACT, {"statement": item["fact"],
                "category": "general", "source_id": str(item.get("uuid", "")),
                "valid_from": item.get("valid_at"), "valid_until": item.get("invalid_at"),
                "expires_at": item.get("expired_at"), "created_at": item.get("created_at")}))
    result.extend(interop.import_zep_episodes({"episodes": document.get("episodes", [])}))
    return result


def convert(vendor: str, document) -> list[dict]:
    converters = {"mem0": interop.import_mem0_dump, "letta": interop.import_letta_blocks,
                  "zep": interop.import_zep_episodes, "graphiti": graphiti_records}
    if vendor not in converters:
        raise ValueError("unsupported vendor")
    rows = converters[vendor](document)
    if len(rows) > 10000:
        raise ValueError("split migration into at most 10000 records")
    return rows


def migrate(root: str, vendor: str, document, *, context: list[str], dry_run: bool = False) -> dict:
    if not isinstance(context, list) or not context or any(
            not isinstance(s, str) or not s.strip() or len(s) > 256 or any(ord(c) < 32 for c in s) for s in context):
        raise ValueError("migration requires explicit owner scopes")
    labels = sorted(set(context))
    encoded = json.dumps(document, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 32*1024*1024:
        raise ValueError("split migration input below 32 MiB")
    rows = convert(vendor, document)
    digest = hashlib.sha256((vendor+encoded+json.dumps(labels)).encode()).hexdigest()
    receipt = os.path.join(paths.memory_dir(root), "migrations", digest+".json")
    counts = {kind: sum(r["kind"] == kind for r in rows) for kind in interop.RECORD_KINDS}
    if dry_run:
        return {"dry_run": True, "digest": digest, "counts": counts}
    paths.enforce_boundary(root, receipt)
    paths.safe_prepare_output_path(receipt)
    with _jsonl.locked(receipt), memory_authority.restricted_writer("migration:"+vendor, "external"):
        if os.path.exists(receipt):
            with open(receipt, encoding="utf-8") as stream:
                return {**json.load(stream), "replayed": True}
        # Preserve the complete original export in a protected local archive. It is
        # not delivered as context or interpreted as tool instructions.
        archive = os.path.join(paths.memory_dir(root), "migrations", digest+".source.json")
        _jsonl.write_json(paths.safe_prepare_output_path(archive), document)
        os.chmod(archive, 0o600)
        source_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "commontrace:migration:"+digest))
        trace_io.write_new(root, trace_id=source_id, title=f"{vendor} migration {digest[:12]}",
            context=f"External {vendor} export SHA256 {hashlib.sha256(encoded.encode()).hexdigest()}",
            solution="Imported records preserve source metadata; no vendor rule is approved by import.",
            tags=["migration", vendor], extra={"scopes": labels})
        applied = skipped = 0
        for index, row in enumerate(rows):
            kind, data = row["kind"], dict(row["data"])
            if kind == interop.KIND_FACT:
                # Imported IDs/revisions cannot overwrite existing local facts.
                fact, _ = hierarchical.append_facts(root, [{"statement": str(data.get("statement", "")),
                    "category": str(data.get("category") or "general"), "scopes": labels,
                    "source_trace_id": source_id, "valid_from": data.get("valid_from") or data.get("created_at"),
                    "valid_until": data.get("valid_until"),
                    "expires_at": data.get("expires_at") or data.get("expiration_date")}])[0]
                applied += bool(fact)
            elif kind in (interop.KIND_BLOCK, interop.KIND_LESSON):
                # Core-memory instructions become review drafts, never active blocks.
                body = data.get("body") or str(data.get("content", ""))
                fm = dict(data.get("frontmatter") or interop._lesson_frontmatter(
                    digest[:12]+"-"+str(index), data.get("name", "Imported memory"), body, "general", vendor))
                fm.update({"scopes": labels, "source_traces": [source_id], "status": "review"})
                data = {"slug": "migration-"+digest[:12]+"-"+str(index), "frontmatter": fm, "body": body}
                applied += interop._apply_lesson(root, data)
            else:
                if kind == interop.KIND_GRAPH_NODE:
                    data["id"] = "migration:"+digest[:24]+":"+data["id"]
                elif kind == interop.KIND_GRAPH_EDGE:
                    data["source"] = "migration:"+digest[:24]+":"+data["source"]
                    data["target"] = "migration:"+digest[:24]+":"+data["target"]
                data.setdefault("properties", {}).update({"scopes": labels, "source_traces": [source_id]})
                summary = interop.apply_store(root, [interop.make_record(kind, data)])
                applied += summary.get(kind, 0)
                skipped += summary.get("skipped", 0)
        result = {"digest": digest, "vendor": vendor, "counts": counts, "applied": applied,
                  "skipped": skipped, "source_trace_id": source_id, "scopes": labels}
        _jsonl.write_json(receipt, result)
        os.chmod(receipt, 0o600)
        return result
