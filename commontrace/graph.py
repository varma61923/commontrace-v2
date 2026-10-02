"""Temporal Knowledge Graph & Multi-Hop Entity Reasoning (Zep + Cognee pattern).

Maintains a bitemporal directed property graph linking services, tools, errors,
concepts, and lessons. Supports multi-hop traversal, entity extraction, and
graph-proximity retrieval boosting.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from commontrace import lesson_cache, paths

ENTITY_TYPES = ("service", "tool", "error", "concept", "lesson", "scope", "user", "file")
RELATIONS = (
    "depends_on",
    "causes",
    "resolves",
    "affects",
    "supersedes",
    "scoped_to",
    "uses",
    "violates",
    "relates_to",
)


@dataclass
class GraphNode:
    id: str
    entity_type: str
    name: str
    properties: dict[str, Any]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphEdge:
    source: str
    target: str
    relation: str
    weight: float
    valid_from: str
    valid_until: str | None
    properties: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _graph_dir(root: str) -> str:
    path = os.path.join(paths.memory_dir(root), "graph")
    os.makedirs(path, exist_ok=True)
    return path


def _nodes_file(root: str) -> str:
    return os.path.join(_graph_dir(root), "nodes.jsonl")


def _edges_file(root: str) -> str:
    return os.path.join(_graph_dir(root), "edges.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_nodes(root: str) -> dict[str, GraphNode]:
    """Load all nodes into memory."""
    fpath = _nodes_file(root)
    nodes: dict[str, GraphNode] = {}
    if not os.path.exists(fpath):
        return nodes

    with open(fpath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                node = GraphNode(**data)
                nodes[node.id] = node
            except Exception:
                continue
    return nodes


def load_edges(root: str) -> list[GraphEdge]:
    """Load all edges into memory."""
    fpath = _edges_file(root)
    edges: list[GraphEdge] = []
    if not os.path.exists(fpath):
        return edges

    with open(fpath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                edge = GraphEdge(**data)
                edges.append(edge)
            except Exception:
                continue
    return edges


def save_nodes(root: str, nodes: dict[str, GraphNode]) -> None:
    fpath = _nodes_file(root)
    tmp_path = f"{fpath}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for node in nodes.values():
            f.write(json.dumps(node.to_dict()) + "\n")
    os.replace(tmp_path, fpath)


def save_edges(root: str, edges: list[GraphEdge]) -> None:
    fpath = _edges_file(root)
    tmp_path = f"{fpath}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for edge in edges:
            f.write(json.dumps(edge.to_dict()) + "\n")
    os.replace(tmp_path, fpath)


def add_node(
    root: str,
    id: str,
    entity_type: str,
    name: str = "",
    properties: dict[str, Any] | None = None,
) -> GraphNode:
    """Add or update an entity node in the graph."""
    clean_id = id.strip().lower()
    if not clean_id:
        raise ValueError("Node ID cannot be empty")
    if entity_type not in ENTITY_TYPES:
        entity_type = "concept"

    nodes = load_nodes(root)
    now_iso = _now()
    if clean_id in nodes:
        node = nodes[clean_id]
        if name:
            node.name = name.strip()
        if properties:
            node.properties.update(properties)
        node.updated_at = now_iso
    else:
        node = GraphNode(
            id=clean_id,
            entity_type=entity_type,
            name=name.strip() or clean_id,
            properties=properties or {},
            created_at=now_iso,
            updated_at=now_iso,
        )
        nodes[clean_id] = node

    save_nodes(root, nodes)
    return node


def add_edge(
    root: str,
    source: str,
    target: str,
    relation: str,
    weight: float = 1.0,
    valid_from: str | None = None,
    valid_until: str | None = None,
    properties: dict[str, Any] | None = None,
) -> GraphEdge:
    """Add a directed relationship between two nodes."""
    src = source.strip().lower()
    dst = target.strip().lower()
    if not src or not dst:
        raise ValueError("Source and target must be non-empty")
    if relation not in RELATIONS:
        relation = "relates_to"

    nodes = load_nodes(root)
    if src not in nodes:
        add_node(root, src, entity_type="concept")
    if dst not in nodes:
        add_node(root, dst, entity_type="concept")

    edges = load_edges(root)
    now_iso = _now()
    valid_from = valid_from or now_iso

    # Look for existing active edge between src, dst and relation
    for edge in edges:
        if edge.source == src and edge.target == dst and edge.relation == relation:
            if edge.valid_until is None:
                edge.weight = max(edge.weight, float(weight))
                if properties:
                    edge.properties.update(properties)
                save_edges(root, edges)
                return edge

    new_edge = GraphEdge(
        source=src,
        target=dst,
        relation=relation,
        weight=round(float(weight), 3),
        valid_from=valid_from,
        valid_until=valid_until,
        properties=properties or {},
        created_at=now_iso,
    )
    edges.append(new_edge)
    save_edges(root, edges)
    return new_edge


def _is_active_edge(edge: GraphEdge, moment: datetime | None) -> bool:
    if not moment:
        return edge.valid_until is None

    if edge.valid_from:
        try:
            vf = lesson_cache.parse_moment(edge.valid_from)
            if vf > moment:
                return False
        except Exception:
            pass
    if edge.valid_until:
        try:
            vu = lesson_cache.parse_moment(edge.valid_until)
            if vu <= moment:
                return False
        except Exception:
            pass
    return True


def get_neighbors(
    root: str,
    node_id: str,
    direction: str = "both",  # "out" | "in" | "both"
    relation: str | None = None,
    as_of: str | None = None,
) -> list[dict[str, Any]]:
    """Return neighbor nodes connected to node_id."""
    clean_id = node_id.strip().lower()
    nodes = load_nodes(root)
    edges = load_edges(root)
    moment = lesson_cache.parse_moment(as_of) if as_of else None

    results: list[dict[str, Any]] = []
    for edge in edges:
        if not _is_active_edge(edge, moment):
            continue
        if relation and edge.relation != relation:
            continue

        if direction in ("out", "both") and edge.source == clean_id:
            target_node = nodes.get(edge.target)
            results.append({
                "neighbor_id": edge.target,
                "neighbor_name": target_node.name if target_node else edge.target,
                "neighbor_type": target_node.entity_type if target_node else "concept",
                "direction": "out",
                "relation": edge.relation,
                "weight": edge.weight,
            })
        elif direction in ("in", "both") and edge.target == clean_id:
            source_node = nodes.get(edge.source)
            results.append({
                "neighbor_id": edge.source,
                "neighbor_name": source_node.name if source_node else edge.source,
                "neighbor_type": source_node.entity_type if source_node else "concept",
                "direction": "in",
                "relation": edge.relation,
                "weight": edge.weight,
            })

    return results


def multi_hop_subgraph(
    root: str,
    start_node_ids: list[str],
    max_hops: int = 2,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Traverse graph up to max_hops from the given start nodes."""
    nodes = load_nodes(root)
    edges = load_edges(root)
    moment = lesson_cache.parse_moment(as_of) if as_of else None

    active_edges = [e for e in edges if _is_active_edge(e, moment)]

    # Adjacency list
    adj: dict[str, list[GraphEdge]] = {}
    for e in active_edges:
        adj.setdefault(e.source, []).append(e)
        adj.setdefault(e.target, []).append(e)

    visited_nodes: dict[str, int] = {}  # node_id -> hop_distance
    collected_edges: list[GraphEdge] = []
    queue: list[tuple[str, int]] = []

    for sid in start_node_ids:
        cid = sid.strip().lower()
        if cid in nodes:
            visited_nodes[cid] = 0
            queue.append((cid, 0))

    while queue:
        curr_id, hop = queue.pop(0)
        if hop >= max_hops:
            continue

        for edge in adj.get(curr_id, []):
            neighbor_id = edge.target if edge.source == curr_id else edge.source
            if edge not in collected_edges:
                collected_edges.append(edge)

            if neighbor_id not in visited_nodes:
                visited_nodes[neighbor_id] = hop + 1
                queue.append((neighbor_id, hop + 1))

    return {
        "nodes": [nodes[nid].to_dict() for nid in visited_nodes if nid in nodes],
        "edges": [e.to_dict() for e in collected_edges],
        "hop_distances": visited_nodes,
    }


def extract_entities_from_text(root: str, text: str) -> list[str]:
    """Extract known graph entity IDs from a free-text string."""
    nodes = load_nodes(root)
    if not nodes or not text.strip():
        return []

    norm_text = text.lower()
    matched_ids: set[str] = set()

    for nid, node in nodes.items():
        # Check node id itself (e.g. 'stripe', 'billing')
        pattern = rf"\b{re.escape(node.name.lower())}\b"
        if re.search(pattern, norm_text):
            matched_ids.add(nid)
            continue
        # Also check without type prefix
        bare = nid.split(":", 1)[-1]
        if len(bare) > 2 and re.search(rf"\b{re.escape(bare)}\b", norm_text):
            matched_ids.add(nid)

    return sorted(matched_ids)


def graph_boost_for_lessons(
    root: str,
    query: str,
    candidate_slugs: list[str],
    as_of: str | None = None,
) -> dict[str, float]:
    """Compute graph-proximity boosts for candidate lesson slugs."""
    entities = extract_entities_from_text(root, query)
    if not entities or not candidate_slugs:
        return {s: 0.0 for s in candidate_slugs}

    subgraph = multi_hop_subgraph(root, start_node_ids=entities, max_hops=2, as_of=as_of)
    hop_distances = subgraph.get("hop_distances", {})

    boosts: dict[str, float] = {}
    for slug in candidate_slugs:
        lesson_node_id = f"lesson:{slug}"
        # Direct hit or neighbor hop
        if lesson_node_id in hop_distances:
            hop = hop_distances[lesson_node_id]
            # Hop 0 = 0.25, Hop 1 = 0.15, Hop 2 = 0.08
            boost = round(0.25 / (1 + hop), 3)
            boosts[slug] = boost
        else:
            boosts[slug] = 0.0

    return boosts


def export_mermaid(root: str, as_of: str | None = None) -> str:
    """Render the active knowledge graph into a clean Mermaid diagram."""
    nodes = load_nodes(root)
    edges = load_edges(root)
    moment = lesson_cache.parse_moment(as_of) if as_of else None

    active_edges = [e for e in edges if _is_active_edge(e, moment)]
    lines = ["graph TD"]

    if not nodes and not active_edges:
        return "graph TD\n  empty[Knowledge graph is empty]"

    # Output node labels with clean styling
    for nid, node in sorted(nodes.items()):
        safe_nid = re.sub(r"[^a-zA-Z0-9_]", "_", nid)
        lines.append(f'  {safe_nid}["{node.name} ({node.entity_type})"]')

    for edge in active_edges:
        src = re.sub(r"[^a-zA-Z0-9_]", "_", edge.source)
        dst = re.sub(r"[^a-zA-Z0-9_]", "_", edge.target)
        lines.append(f"  {src} -->|{edge.relation}| {dst}")

    return "\n".join(lines)


def export_json(root: str, as_of: str | None = None) -> dict[str, Any]:
    """Export the graph as structured JSON for agent and MCP consumption."""
    nodes = load_nodes(root)
    edges = load_edges(root)
    moment = lesson_cache.parse_moment(as_of) if as_of else None
    active_edges = [e for e in edges if _is_active_edge(e, moment)]

    return {
        "nodes": [n.to_dict() for n in nodes.values()],
        "edges": [e.to_dict() for e in active_edges],
        "total_nodes": len(nodes),
        "total_active_edges": len(active_edges),
    }
