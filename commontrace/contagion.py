"""UniMem bounded read/write contagion and lineage-root diversity diagnostics."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from commontrace import frontier_protocol as fp
from commontrace import lineage
from commontrace import nextwave_store as store

MAX_NODES = 2000
MAX_DEPTH = 16
KIND = "read-graph"


def record_read(
    root: str,
    memories: Sequence[str],
    agent: str,
    episode: str,
    output: str,
    *,
    context: Sequence[str] = (),
    retention_days: int = 7,
) -> dict[str, Any]:
    fp.count(len(memories), 1, 256)
    fp.count(retention_days, 1, 30)
    ids = sorted(set(memories))
    admitted = store.admit(root, ids, context=context)
    if set(admitted) != set(ids):
        raise PermissionError("UniMem read graph requires admitted input memories")
    # Caller context is a ceiling, not an artifact's inherited classification.
    scopes = sorted({s for value in admitted.values() for s in value.get("scopes", [])})
    value = {
        "memories": ids,
        "agent": fp.text(agent),
        "episode": fp.text(episode),
        "output": fp.text(output),
        "expires_at": (datetime.fromisoformat(fp.now()) + timedelta(days=retention_days)).isoformat(),
    }
    identity = fp.digest([ids, agent, episode, output])
    prior = store.latest(root, KIND).get(identity)
    if prior:
        # A retry preserves its original retention deadline, never extends it.
        old = store.get(root, KIND, identity, context=context)
        return {"id": identity, **old}
    store.put(root, KIND, identity, value, scopes=scopes, sources=ids, immutable=True)
    return {"id": identity, **value}


def _visible_nodes(root: str, context: Sequence[str]) -> tuple[set[str], dict[str, list[str]], dict[str, str]]:
    records = lineage.records(root)
    fp.count(len(records), 0, MAX_NODES, "UniMem scoped lineage graph")
    admitted = {}
    ids = list(records)
    for start in range(0, len(ids), 512):
        admitted.update(store.admit(root, ids[start : start + 512], context=context))
    visible = set(admitted)
    edges: dict[str, list[str]] = {}
    kinds = dict.fromkeys(visible, "memory")
    for identity in visible:
        for parent in records[identity].get("inputs", []):
            if parent["id"] in visible:
                edges.setdefault(parent["id"], []).append(identity)
    from pathlib import Path

    from commontrace import paths

    journals = sorted(Path(paths.memory_dir(root)).glob("unimem-*.jsonl"))
    fp.count(len(journals), 0, 128, "UniMem artifact namespaces")
    for journal in journals:
        kind = journal.name[len("unimem-") : -len(".jsonl")]
        for identity, metadata in store.latest(root, kind).items():
            if metadata.get("status") != "active" or not store.visible(metadata.get("scopes", []), context):
                continue
            if "digest" not in metadata:
                continue
            try:
                value = store.get(root, kind, identity, context=context)
            except (ValueError, PermissionError):
                continue
            if kind == KIND and value["expires_at"] <= fp.now():
                continue
            node = store.reference(kind, identity)
            visible.add(node)
            kinds[node] = kind
            fp.count(len(visible), 0, MAX_NODES, "UniMem visible graph nodes")
            for parent in metadata.get("sources", []):
                edges.setdefault(parent, []).append(node)
            if kind == KIND and value["expires_at"] > fp.now():
                for field in ("agent", "episode", "output"):
                    child = field + ":" + value[field]
                    visible.add(child)
                    kinds[child] = field
                    edges.setdefault(node, []).append(child)
                    fp.count(len(visible), 0, MAX_NODES, "UniMem visible graph nodes")
    return visible, edges, kinds


def radius(
    root: str, identity: str, *, context: Sequence[str] = (), max_depth: int = MAX_DEPTH, max_nodes: int = MAX_NODES
) -> dict[str, Any]:
    fp.count(max_depth, 1, MAX_DEPTH)
    fp.count(max_nodes, 1, MAX_NODES)
    if identity not in store.admit(root, [identity], context=context):
        raise PermissionError("UniMem contagion source is not admitted")
    visible, edges, kinds = _visible_nodes(root, context)
    seen = {identity}
    pending = deque([(identity, 0)])
    nodes, truncated = [], False
    while pending:
        node, depth = pending.popleft()
        nodes.append({"id": node, "kind": kinds.get(node, "memory"), "depth": depth})
        children = sorted(set(edges.get(node, [])) & visible - seen)
        if depth == max_depth:
            truncated |= bool(children)
            continue
        for child in children:
            if len(seen) >= max_nodes:
                truncated = True
                break
            seen.add(child)
            pending.append((child, depth + 1))
    return {
        "source": identity,
        "nodes": nodes,
        "truncated": truncated,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "classification": "observed dependency graph",
        "unobserved_reads": "not covered",
    }


def freeze(root: str, identity: str, incident_id: str, *, context: Sequence[str] = ()) -> dict[str, Any]:
    from commontrace import incident

    store.operator()
    affected = radius(root, identity, context=context)
    # Full incident snapshot is an operator-only evidence boundary; the scoped
    # radius itself contains no invisible nodes.
    frozen = incident.freeze(root, incident_id, fp.now())
    store.event(root, "contagion-freezes", incident_id, {"source": identity, "radius_digest": fp.digest(affected)})
    return {"radius": affected, "incident": frozen}


def independence(
    root: str, identities: Sequence[str], *, context: Sequence[str] = (), downweight: bool = False
) -> dict[str, Any]:
    fp.count(len(identities), 0, 512)
    admitted = store.admit(root, identities, context=context)
    roots = {i: sorted(set(lineage.record(root, i).get("root_origins", []))) or ["unknown"] for i in admitted}
    weights: dict[str, float] = {}
    for sources in roots.values():
        for source in sources:
            weights[source] = weights.get(source, 0) + 1 / len(sources)
    total = sum(weights.values())
    effective = total * total / sum(v * v for v in weights.values()) if total else 0
    return {
        "n_eff": effective,
        "records": [
            {
                "id": i,
                "roots": sources,
                "agreement_weight": sum(1 / weights[s] for s in sources) / len(sources) if downweight else 1,
            }
            for i, sources in roots.items()
        ],
        "classification": "lineage diversity estimate",
        "assumptions": "authenticated roots are not proof of independent organizations; unknown roots share one group",
    }


def reretrieve(
    root: str,
    query: str,
    identities: Sequence[str],
    *,
    minimum: float = 2,
    context: Sequence[str] = (),
    retriever: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    from commontrace import search_recipes

    fp.text(query, "query", 2000)
    fp.number(minimum, 1, 100)
    initial = independence(root, identities, context=context)
    ids = [r["id"] for r in initial["records"]]
    triggered = initial["n_eff"] < minimum
    if triggered:
        proposed = (retriever or search_recipes.search)(root, query, context=list(context), limit=100)
        if not isinstance(proposed, list) or len(proposed) > 512:
            raise ValueError("UniMem independent-source retriever exceeded its bound")
        fresh = store.admit(root, [fp.text(r["id"]) for r in proposed], context=context)
        ids = sorted(set(ids) | set(fresh))
    result = independence(root, ids, context=context, downweight=True)
    return {**result, "triggered": triggered, "threshold": minimum, "threshold_met": result["n_eff"] >= minimum}


def prune(root: str) -> dict[str, Any]:
    """Remove expired read payloads; retain signed metadata commitments for audit."""
    import json
    import os

    from commontrace import _jsonl

    store.operator()
    removed = []
    with _jsonl.locked(fp.filename(root, store.journal_name(KIND))):
        for identity, row in store.latest(root, KIND).items():
            if row.get("status") != "active":
                continue
            filename = store._blob(root, KIND, row["digest"])
            try:
                with open(filename, "rb") as stream:
                    raw = stream.read(store.MAX_BLOB + 2)
            except FileNotFoundError:
                continue
            if len(raw) > store.MAX_BLOB + 1:
                raise ValueError("UniMem read payload exceeds its bound")
            value = json.loads(raw)
            if fp.digest(value) != row["digest"]:
                raise ValueError("UniMem read payload digest mismatch")
            if value["expires_at"] > fp.now():
                continue
            os.unlink(filename)
            store.event(
                root,
                KIND,
                identity,
                {"status": "expired", "digest": row["digest"], "scopes": row["scopes"], "sources": row["sources"]},
            )
            removed.append(identity)
    return {"expired": removed, "retained": "signed scope/source commitments, not read payloads"}
