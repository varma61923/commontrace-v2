"""Provenance evidence log for graph nodes/edges."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from commontrace import _jsonl, paths


def _provenance_file(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "graph", "provenance.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_provenance(
    root: str,
    target_kind: str,
    target_id: str,
    source_path: str = "",
    run_id: str = "",
    detail: Any = "",
) -> dict[str, Any]:
    """Append a provenance record and return it."""
    fpath = _provenance_file(root)
    record = {
        "target_kind": str(target_kind),
        "target_id": str(target_id),
        "source_path": str(source_path or ""),
        "run_id": str(run_id or ""),
        "detail": detail if isinstance(detail, (dict, list)) else str(detail or ""),
        "created_at": _now(),
    }
    _jsonl.append_row(fpath, record)
    return record


def list_provenance(root: str, target_id: str) -> list[dict[str, Any]]:
    """Return all provenance records matching target_id (case-insensitive)."""
    fpath = _provenance_file(root)
    if not os.path.exists(fpath):
        return []
    want = str(target_id).strip().lower()
    out: list[dict[str, Any]] = []
    with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            tid = str(rec.get("target_id", "")).strip().lower()
            if tid == want:
                out.append(rec)
    out.sort(key=lambda r: str(r.get("created_at", "")))
    return out


# --- lineage --------------------------------------------------------------------------
#
# Derivation edges point from a source to what was derived from it ("child came
# from parent"). They are gathered from every place the store records origin:
#
#   provenance.jsonl   source_path -> target_id, and run:<run_id> -> target_id
#   facts              source_traces and evidence receipts -> fact; a superseded
#                      fact -> the fact that replaced it
#   observations       source_fact_ids and evidence source ids -> observation
#   control records    data.sources -> proposal / foresight / wiki / ... record
#   lessons            source_traces -> lesson slug
#
# Traversal is breadth-first, cycle-safe (each node is expanded once) and bounded
# by depth and by node count; ``truncated`` says when a bound cut it short.

DEFAULT_MAX_DEPTH = 5
MAX_DEPTH = 32
MAX_NODES = 2000
_CONTROL_KINDS = ("proposal", "foresight", "wiki", "compression", "mental-model")


def _node_kind(node_id: str) -> str:
    lowered = node_id.lower()
    for prefix, kind in (("fact-", "fact"), ("obs-", "observation"), ("fcl-", "fact-cluster"),
                         ("run:", "run"), ("file:", "file"), ("lesson:", "lesson"), ("chunk", "chunk")):
        if lowered.startswith(prefix):
            return kind
    if "->" in node_id:
        return "edge"
    if "/" in node_id or os.sep in node_id:
        return "path"
    return "record"


def _derivation_edges(root: str) -> list[tuple[str, str, str]]:
    """Every (parent, child, relation) the store records, deduplicated and sorted."""
    edges: set[tuple[str, str, str]] = set()

    def add(parent, child, relation: str) -> None:
        parent, child = str(parent or "").strip(), str(child or "").strip()
        if parent and child and parent != child:
            edges.add((parent, child, relation))

    for rec in _jsonl.read_rows(_provenance_file(root)):
        target = rec.get("target_id")
        kind = str(rec.get("target_kind") or "record")
        add(rec.get("source_path"), target, f"provenance:{kind}")
        if rec.get("run_id"):
            add("run:" + str(rec["run_id"]), target, f"run:{kind}")

    from commontrace import hierarchical

    try:
        facts = hierarchical.load_facts(root)
    except (OSError, ValueError):
        facts = {}
    for fact in facts.values():
        for trace in fact.source_traces:
            add(trace, fact.id, "source_trace")
        for receipt in fact.evidence:
            add(receipt.source_id, fact.id, f"evidence:{receipt.kind}:{receipt.polarity}")
        if fact.superseded_by:
            add(fact.id, fact.superseded_by, "superseded_by")

    from commontrace import observations

    for row in _jsonl.read_rows(observations._observations_file(root)):
        oid = row.get("id")
        for source in row.get("source_fact_ids") or []:
            add(source, oid, "observation_of")
        for evidence in row.get("evidence") or []:
            if isinstance(evidence, dict):
                add(evidence.get("source_id"), oid, "observation_evidence")

    from commontrace import memory_control

    for kind in _CONTROL_KINDS:
        try:
            rows = memory_control.records(root, kind)
        except (OSError, ValueError, PermissionError):
            continue
        for row in rows:
            for source in (row.get("data") or {}).get("sources") or []:
                add(source, row.get("id"), f"control:{kind}")

    lessons = paths.lessons_dir(root)
    if os.path.isdir(lessons):
        from commontrace import frontmatter

        for name in sorted(os.listdir(lessons)):
            if not (name.startswith("lesson_") and name.endswith(".md")):
                continue
            try:
                fm, _body = frontmatter.read(os.path.join(lessons, name))
            except Exception:  # noqa: BLE001 - one unreadable lesson must not hide the rest
                continue
            slug = str(fm.get("name") or name[len("lesson_"):-3])
            for trace in fm.get("source_traces") or []:
                add(trace, "lesson:" + slug, "lesson_source")
    return sorted(edges)


def lineage(root: str, record_id: str, direction: str = "down", max_depth: int = DEFAULT_MAX_DEPTH, *,
            max_nodes: int = MAX_NODES) -> dict[str, Any]:
    """The derivation graph around *record_id*.

    ``direction="down"``: everything derived from it (its descendants);
    ``"up"``: everything it came from (its ancestors). Lessons are addressed as
    ``lesson:<slug>``. Ids match exactly, falling back to a case-insensitive match.
    """
    if direction not in ("down", "up"):
        raise ValueError("direction must be down or up")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 1 <= max_depth <= MAX_DEPTH:
        raise ValueError(f"max_depth must be an integer from 1 to {MAX_DEPTH}")
    if not str(record_id or "").strip():
        raise ValueError("record id must not be empty")
    edges = _derivation_edges(root)
    forward: dict[str, list[tuple[str, str]]] = {}
    for parent, child, relation in edges:
        if direction == "down":
            forward.setdefault(parent, []).append((child, relation))
        else:
            forward.setdefault(child, []).append((parent, relation))
    start = str(record_id).strip()
    known = {n for p, c, _r in edges for n in (p, c)}
    if start not in known:
        folded = {n.lower(): n for n in sorted(known, reverse=True)}
        start = folded.get(start.lower(), start)
    depth_of = {start: 0}
    frontier = [start]
    out_edges: list[dict[str, Any]] = []
    revisited = 0
    truncated = False
    for depth in range(1, max_depth + 1):
        following: list[str] = []
        for node in frontier:
            for other, relation in forward.get(node, []):
                source, target = (node, other) if direction == "down" else (other, node)
                out_edges.append({"source": source, "target": target, "relation": relation})
                if other in depth_of:
                    if depth_of[other] < depth:
                        revisited += 1
                    continue
                if len(depth_of) >= max_nodes:
                    truncated = True
                    continue
                depth_of[other] = depth
                following.append(other)
        frontier = following
        if not frontier:
            break
    else:
        truncated = truncated or any(other not in depth_of for node in frontier for other, _r in forward.get(node, []))
    nodes = [{"id": node, "kind": _node_kind(node), "depth": depth} for node, depth in
             sorted(depth_of.items(), key=lambda kv: (kv[1], kv[0]))]
    out_edges = [e for e in out_edges if e["source"] in depth_of and e["target"] in depth_of]
    return {"root": start, "direction": direction, "max_depth": max_depth, "found": start in known,
            "nodes": nodes, "edges": out_edges, "revisited": revisited, "truncated": truncated}


def render_lineage(result: dict[str, Any]) -> str:
    """An indented tree of the lineage (a node reached twice is printed once)."""
    if not result["found"]:
        return f"No lineage recorded for '{result['root']}'."
    arrow = "derived" if result["direction"] == "down" else "came from"
    children: dict[str, list[tuple[str, str]]] = {}
    for edge in result["edges"]:
        parent, child = (edge["source"], edge["target"]) if result["direction"] == "down" else \
            (edge["target"], edge["source"])
        children.setdefault(parent, []).append((child, edge["relation"]))
    lines = [f"{result['root']}  ({len(result['nodes']) - 1} record(s) {arrow}, depth <= {result['max_depth']})"]
    printed = {result["root"]}

    def walk(node: str, indent: int) -> None:
        for child, relation in children.get(node, []):
            seen = child in printed
            lines.append("  " * indent + f"{'<-' if result['direction'] == 'up' else '->'} {child}  "
                         f"[{relation}]" + ("  (already shown)" if seen else ""))
            if not seen:
                printed.add(child)
                walk(child, indent + 1)

    walk(result["root"], 1)
    if result["truncated"]:
        lines.append("(truncated: raise --max-depth to see further)")
    return "\n".join(lines)
