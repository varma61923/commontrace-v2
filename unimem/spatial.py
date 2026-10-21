"""Bounded object deltas and two-clock planner queries; dense geometry stays at edge.

The interchange fields accept Hydra/HOV-SG/ConceptGraphs/DynaMem-style object
summaries without claiming binary compatibility with their native formats.
All queries authorize tenant/site/robot labels before matching objects. Event
validity and record time are distinct; corrections never erase prior beliefs.
"""
from __future__ import annotations

import copy
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

from unimem import _jsonl, hierarchical, lineage, memory_authority, memory_ifc, search_recipes
from unimem import frontier_protocol as fp
from unimem import transparency_log as log

JOURNAL = "spatial-deltas.jsonl"
RELATIONS = {"located_in", "on", "near", "seen_at"}


def _context(context: dict[str, str]) -> list[str]:
    if not set(context) <= {"tenant", "site", "robot"} or not {"tenant", "site"} <= set(context):
        raise ValueError("spatial context requires tenant and site, with optional robot")
    return [key + ":" + fp.text(value, key, 64) for key, value in sorted(context.items())]


def _visible(row: dict[str, Any], context: dict[str, str]) -> bool:
    # Robot is an optional restriction, while tenant/site are mandatory boundaries.
    return all(row["context"].get(key) == value for key, value in context.items())


# Re-observing an object within this distance (frame units, typically metres) and
# quaternion-component tolerance of its last placement reinforces that placement's
# fact instead of minting a new one: facts grow with moves, not with sightings.
PLACEMENT_TOLERANCE = 0.01


@dataclass
class _Spatial:
    count: int = 0
    tip: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    latest: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)


_STATES: OrderedDict[str, _Spatial] = OrderedDict()


def _after_fork() -> None:
    _STATES.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _state(root: str) -> _Spatial:
    """Verified journal rows, indexed, extended incrementally per signed checkpoint."""
    key = os.path.realpath(root)
    count, tip = fp.checkpoint(root, JOURNAL)
    with fp._GUARD:
        cached = _STATES.get(key)
        if cached is not None and cached.count == count and cached.tip == tip:
            _STATES.move_to_end(key)
            return cached
        if cached is None or count < cached.count or (cached.count and fp.digest(
                fp.signed_at(root, JOURNAL, cached.count - 1)) != cached.tip):
            cached = _Spatial()
        count, tip, events = fp.event_snapshot(root, JOURNAL, cached.count)
        for row in events:
            cached.rows.append(row)
            cached.by_id[row["id"]] = row
            cache_key = (row["context"]["tenant"], row["context"]["site"], row["delta"]["object_id"])
            prior = cached.latest.get(cache_key)
            if prior is None or fp.moment(row["delta"]["timestamp"]) >= fp.moment(prior["delta"]["timestamp"]):
                cached.latest[cache_key] = row
        cached.count, cached.tip = count, tip
        _STATES[key] = cached
        _STATES.move_to_end(key)
        while len(_STATES) > 16:
            _STATES.popitem(last=False)
        return cached


def _same_placement(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return (before["frame"] == after["frame"] and before["class"] == after["class"]
            and len(before["pose"]) == len(after["pose"])
            and all(abs(a - b) <= PLACEMENT_TOLERANCE for a, b in zip(before["pose"], after["pose"])))


def update(root: str, delta: dict[str, Any], *, context: dict[str, str],
           correction_of: str | None = None) -> dict[str, Any]:
    from unimem.household import assert_spatial_persistable

    assert_spatial_persistable(root, delta, context)
    # Household identity proof is an admission token, not scene content. Never
    # duplicate it into the spatial journal after the pre-write gate succeeds.
    delta = {key: value for key, value in delta.items()
             if key not in {"household_gate", "contains_person", "room_id"}}
    scopes = _context(context)
    required = {"object_id", "class", "pose", "frame", "robot_id", "timestamp", "confidence"}
    allowed = required | {"embedding_ref", "relations", "keyframe", "media", "room_id",
                          "contains_person", "household_gate"}
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
        if not isinstance(targets, list):
            raise ValueError("scene-graph relation targets must be a list")
        fp.count(len(targets), 0, 16)
        for target in targets:
            fp.text(target, "relation object", 128)
    for key in ("embedding_ref", "keyframe"):
        if key in delta:
            fp.text(delta[key], key, 256)
    media = delta.get("media", [])
    if not isinstance(media, list):
        raise ValueError("media references must be a list")
    fp.count(len(media), 0, 8)
    for reference in media:
        fp.text(reference, "content-addressed media reference", 256)
    size = len(fp.wire(delta))
    if size > 4096:
        raise ValueError("object delta exceeds 4 KiB; keep geometry on the robot")
    with _jsonl.locked(fp.filename(root, JOURNAL)):
        state = _state(root)
        if correction_of:
            with fp._GUARD:
                original = state.by_id.get(correction_of)
            if (original is None or original["context"] != context
                    or original["delta"]["object_id"] != delta["object_id"]):
                raise PermissionError("correction must refer to the same authenticated object and owner")
        minute = datetime.fromisoformat(fp.now()) - timedelta(minutes=1)
        with fp._GUARD:
            recent = 0
            for row in reversed(state.rows):  # rows are appended in record-time order
                if datetime.fromisoformat(row["recorded_at"]) < minute:
                    break
                if row["context"]["tenant"] == context["tenant"]:
                    recent += row["wire_bytes"]
        if recent + size > 64 * 1024:
            raise ValueError("tenant spatial update budget of 64 KiB per minute exceeded")
        identity = "spatial-" + fp.digest({"context": context, "delta": delta, "correction": correction_of})[:32]
        with fp._GUARD:
            prior = state.by_id.get(identity)
        if prior:
            return copy.deepcopy(prior)
        cache_key = (context["tenant"], context["site"], delta["object_id"])
        with fp._GUARD:
            previous = state.latest.get(cache_key)
        reused = None
        if previous is not None and not correction_of and _same_placement(previous["delta"], delta):
            # Reinforce only a placement fact that is still admissible now.
            if previous["fact_id"] in search_recipes.admit(root, [previous["fact_id"]], context=scopes):
                reused = previous["fact_id"]
        if reused is None:
            statement = (f"Object {delta['object_id']} ({delta['class']}) at {delta['pose']} in {delta['frame']} "
                         f"observed by {delta['robot_id']} at {delta['timestamp']}; observation {identity}")
            fact, _ = hierarchical.add_fact(root, statement, scopes=scopes, valid_from=delta["timestamp"])
            reused = fact.id
        body = {"id": identity, "fact_id": reused, "context": context, "scopes": scopes,
                "delta": delta, "correction_of": correction_of, "wire_bytes": size}
        lineage.attest(root, body, fp.sign(root, body), inputs=[], operation="spatial-object-delta")
        signed = fp.append(root, JOURNAL, body)
        log.append(root, "spatial-update", {"id": identity, "digest": fp.digest(signed)}, receipt=False)
        return cast(dict[str, Any], signed["record"])


def history(root: str, object_id: str | None = None, *, context: dict[str, str],
            known_at: str | None = None, since: str | None = None,
            as_of: str | None = None) -> list[dict[str, Any]]:
    scopes = _context(context)
    if object_id is not None:
        fp.text(object_id, "object id", 128)
    known = fp.moment(known_at) if known_at else fp.now()
    start = fp.moment(since) if since else None
    with fp._GUARD:
        snapshot = list(_state(root).rows)
    candidates = [row for row in snapshot if _visible(row, context) and row["recorded_at"] <= known
                  and (object_id is None or row["delta"]["object_id"] == object_id)
                  and (start is None or row["recorded_at"] > start)]
    admitted = search_recipes.admit(root, sorted({r["fact_id"] for r in candidates}),
                                    context=scopes, as_of=as_of, known_at=known_at)
    rows = []
    for row in candidates:
        if row["fact_id"] not in admitted:
            continue
        if memory_authority.lineage_blocked(root, row["fact_id"]) or memory_authority.lineage_blocked(root, row["id"]):
            continue
        rows.append(memory_ifc.safe_label(root, {**copy.deepcopy(row),
                                                 "text": f"{row['delta']['object_id']} at {row['delta']['pose']} "
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
    at = fp.moment(time)
    # Admit the historical snapshot once. Re-running history/where_is for every
    # object repeats the full journal scan and source admission quadratically.
    rows = history(root, context=context, known_at=at, as_of=at)
    corrected = {row["correction_of"] for row in rows if row.get("correction_of")}
    latest: dict[str, dict[str, Any]] = {}
    positions: dict[str, tuple[str, int]] = {}
    for row in rows:
        observed = fp.moment(row["delta"]["timestamp"])
        if row["id"] in corrected or observed > at:
            continue
        identity = row["delta"]["object_id"]
        position = observed, row["sequence"]
        if identity not in positions or position > positions[identity]:
            positions[identity], latest[identity] = position, row
    return [latest[identity] for identity in sorted(latest)]


def changes_since(root: str, time: str, *, context: dict[str, str]) -> list[dict[str, Any]]:
    return history(root, context=context, since=time)


def sharing_record(row: dict[str, Any], *, tenant: str) -> Any:
    """Use the existing tenant-pinned Hub PostgresJournal.put, never an unscoped map endpoint."""
    from unimem.memory_record import MemoryRecord

    if row["context"]["tenant"] != tenant:
        raise PermissionError("site maps cannot cross tenants")
    return MemoryRecord(id=row["id"], kind="entity", version=1,
        payload={"text": row["text"] if "text" in row else str(row["delta"]), "spatial": row["delta"]},
        scope={"tenant": tenant, "labels": ["site:" + row["context"]["site"]]},
        provenance={"format": "unimem.spatial-object.v1", "lineage_id": row["id"]},
        valid_from=row["delta"]["timestamp"], created_at=row["recorded_at"])
