"""Bounded object deltas and two-clock planner queries; dense geometry stays at edge.

The interchange fields accept Hydra/HOV-SG/ConceptGraphs/DynaMem-style object
summaries without claiming binary compatibility with their native formats.
All queries authorize tenant/site/robot labels before matching objects. Event
validity and record time are distinct; corrections never erase prior beliefs.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

from commontrace import _jsonl, hierarchical, lineage, memory_authority, memory_ifc, search_recipes
from commontrace import frontier_protocol as fp
from commontrace import transparency_log as log

JOURNAL = "spatial-deltas.jsonl"
RELATIONS = {"located_in", "on", "near", "seen_at"}


def _context(context: dict[str, str]) -> list[str]:
    if not set(context) <= {"tenant", "site", "robot"} or not {"tenant", "site"} <= set(context):
        raise ValueError("spatial context requires tenant and site, with optional robot")
    return [key + ":" + fp.text(value, key, 64) for key, value in sorted(context.items())]


def _visible(row: dict[str, Any], context: dict[str, str]) -> bool:
    # Robot is an optional restriction, while tenant/site are mandatory boundaries.
    return all(row["context"].get(key) == value for key, value in context.items())


def update(root: str, delta: dict[str, Any], *, context: dict[str, str],
           correction_of: str | None = None) -> dict[str, Any]:
    scopes = _context(context)
    required = {"object_id", "class", "pose", "frame", "robot_id", "timestamp", "confidence"}
    allowed = required | {"embedding_ref", "relations", "keyframe", "media"}
    if not required <= set(delta) or not set(delta) <= allowed:
        raise ValueError("spatial delta requires bounded object, class, pose/frame, robot, timestamp and confidence")
    for key in ("object_id", "class", "frame", "robot_id"):
        fp.text(delta[key], key, 128)
    if "robot" in context and delta["robot_id"] != context["robot"]:
        raise PermissionError("robot context differs from delta owner")
    fp.moment(delta["timestamp"])
    fp.number(delta["confidence"], 0, 1)
    pose = delta["pose"]
    if not isinstance(pose, list) or len(pose) not in (3, 7):
        raise ValueError("pose must contain xyz or xyz plus xyzw quaternion")
    for value in pose:
        fp.number(value, -1e7, 1e7)
    relations = delta.get("relations", {})
    if not isinstance(relations, dict) or not set(relations) <= RELATIONS:
        raise ValueError("unsupported scene-graph relation")
    for targets in relations.values():
        fp.count(len(targets), 0, 16)
        for target in targets:
            fp.text(target, "relation object", 128)
    for key in ("embedding_ref", "keyframe"):
        if key in delta:
            fp.text(delta[key], key, 256)
    fp.count(len(delta.get("media", [])), 0, 8)
    for reference in delta.get("media", []):
        fp.text(reference, "content-addressed media reference", 256)
    if len(fp.wire(delta)) > 4096:
        raise ValueError("object delta exceeds 4 KiB; keep geometry on the robot")
    with _jsonl.locked(fp.filename(root, JOURNAL)):
        rows = fp.journal(root, JOURNAL)
        if correction_of:
            original = next((r for r in rows if r["id"] == correction_of), None)
            if (original is None or original["context"] != context
                    or original["delta"]["object_id"] != delta["object_id"]):
                raise PermissionError("correction must refer to the same authenticated object and owner")
        minute = datetime.fromisoformat(fp.now()) - timedelta(minutes=1)
        size = sum(r["wire_bytes"] for r in rows if r["context"]["tenant"] == context["tenant"]
                   and datetime.fromisoformat(r["recorded_at"]) >= minute)
        if size + len(fp.wire(delta)) > 64 * 1024:
            raise ValueError("tenant spatial update budget of 64 KiB per minute exceeded")
        identity = "spatial-" + fp.digest({"context": context, "delta": delta, "correction": correction_of})[:32]
        prior = next((r for r in rows if r["id"] == identity), None)
        if prior:
            return prior
        statement = (f"Object {delta['object_id']} ({delta['class']}) at {delta['pose']} in {delta['frame']} "
                     f"observed by {delta['robot_id']} at {delta['timestamp']}; observation {identity}")
        fact, _ = hierarchical.add_fact(root, statement, scopes=scopes, valid_from=delta["timestamp"])
        body = {"id": identity, "fact_id": fact.id, "context": context, "scopes": scopes,
                "delta": delta, "correction_of": correction_of, "wire_bytes": len(fp.wire(delta))}
        lineage.attest(root, body, fp.sign(root, body), inputs=[], operation="spatial-object-delta")
        signed = fp.append(root, JOURNAL, body)
        log.append(root, "spatial-update", {"id": identity, "digest": fp.digest(signed)})
        return cast(dict[str, Any], signed["record"])


def history(root: str, object_id: str | None = None, *, context: dict[str, str],
            known_at: str | None = None, since: str | None = None,
            as_of: str | None = None) -> list[dict[str, Any]]:
    scopes = _context(context)
    if object_id is not None:
        fp.text(object_id, "object id", 128)
    known = fp.moment(known_at) if known_at else fp.now()
    start = fp.moment(since) if since else None
    candidates = [row for row in fp.journal(root, JOURNAL) if _visible(row, context) and row["recorded_at"] <= known]
    admitted = search_recipes.admit(root, [r["fact_id"] for r in candidates],
                                    context=scopes, as_of=as_of, known_at=known_at)
    rows = []
    for row in candidates:
        if row["fact_id"] not in admitted:
            continue
        if memory_authority.lineage_blocked(root, row["fact_id"]) or memory_authority.lineage_blocked(root, row["id"]):
            continue
        if object_id is not None and row["delta"]["object_id"] != object_id:
            continue
        if start and row["recorded_at"] <= start:
            continue
        rows.append(memory_ifc.label(root, {**row, "text": f"{row['delta']['object_id']} at {row['delta']['pose']} "
                                          f"in {row['delta']['frame']}"}))
    return rows


def where_is(root: str, object_id: str, *, context: dict[str, str], when: str | None = None,
             known_at: str | None = None) -> dict[str, Any] | None:
    at = fp.moment(when) if when else fp.now()
    rows = history(root, object_id, context=context, known_at=known_at, as_of=at)
    corrected = {r["correction_of"] for r in rows if r.get("correction_of")}
    rows = [r for r in rows if r["id"] not in corrected and fp.moment(r["delta"]["timestamp"]) <= at]
    return max(rows, key=lambda r: (fp.moment(r["delta"]["timestamp"]), r["sequence"]), default=None)


def where_was(root: str, object_id: str, when: str, *, context: dict[str, str]) -> dict[str, Any] | None:
    return where_is(root, object_id, context=context, when=when)


def believed_at(root: str, robot: str, time: str, *, context: dict[str, str]) -> list[dict[str, Any]]:
    context = {**context, "robot": fp.text(robot, "robot", 64)}
    rows = history(root, context=context, known_at=time)
    return [result for identity in sorted({r["delta"]["object_id"] for r in rows})
            if (result := where_is(root, identity, context=context, when=time, known_at=time)) is not None]


def changes_since(root: str, time: str, *, context: dict[str, str]) -> list[dict[str, Any]]:
    return history(root, context=context, since=time)


def sharing_record(row: dict[str, Any], *, tenant: str) -> Any:
    """Use the existing tenant-pinned Hub PostgresJournal.put, never an unscoped map endpoint."""
    from commontrace.memory_record import MemoryRecord

    if row["context"]["tenant"] != tenant:
        raise PermissionError("site maps cannot cross tenants")
    return MemoryRecord(id=row["id"], kind="entity", version=1,
        payload={"text": row["text"] if "text" in row else str(row["delta"]), "spatial": row["delta"]},
        scope={"tenant": tenant, "labels": ["site:" + row["context"]["site"]]},
        provenance={"format": "commontrace.spatial-object.v1", "lineage_id": row["id"]},
        valid_from=row["delta"]["timestamp"], created_at=row["recorded_at"])
