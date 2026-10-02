"""Temporal Knowledge Graph & Multi-Hop Entity Reasoning (Zep + Cognee pattern).

Maintains a bitemporal directed property graph linking services, tools, errors,
concepts, and lessons. Supports multi-hop traversal, entity extraction, and
graph-proximity retrieval boosting.
"""
from __future__ import annotations

import json
import logging
import os
import re
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from commontrace import lesson_cache, paths

ENTITY_TYPES = ("service", "tool", "error", "concept", "lesson", "scope", "user", "file", "memory", "document")
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
    # Memory relationship types (from Supermemory)
    "updates",
    "extends",
    "derives",
)


@dataclass
class GraphNode:
    id: str
    entity_type: str
    name: str
    properties: dict[str, Any]
    created_at: str
    updated_at: str
    # Version chain fields for memory evolution (Supermemory pattern)
    parent_id: str | None = None  # parentMemoryId
    root_id: str | None = None  # rootMemoryId
    version: int = 1  # version number
    is_latest: bool = True  # isLatest flag
    is_forgotten: bool = False  # isForgotten flag

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphEdge:
    source: str
    target: str
    relation: str
    weight: float
    valid_at: str | None  # When fact becomes true (transaction time)
    invalid_at: str | None  # When fact becomes false (transaction time)
    expired_at: str | None  # When record expires (validity time)
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


# In-process adjacency memo: key (abs root, nodes stamp, edges stamp, as_of)
# -> (nodes, active_edges, adjacency). Invalidated by file stamps, so graph
# mutations (which rewrite nodes.jsonl/edges.jsonl) are picked up. Bounded to
# avoid unbounded growth across many stores.
_GRAPH_ADJ_CACHE: dict[tuple, tuple] = {}
_GRAPH_ADJ_CACHE_MAX_ENTRIES = 64


def _clear_graph_cache() -> None:
    """Drop all memoized adjacency (primarily for tests)."""
    _GRAPH_ADJ_CACHE.clear()


def _graph_files_stamp(root: str) -> tuple[int, int, int, int]:
    """Return (nodes_mtime_ns, nodes_size, edges_mtime_ns, edges_size)."""
    try:
        nst = os.stat(_nodes_file(root))
        nodes_stamp = (int(nst.st_mtime_ns), int(nst.st_size))
    except OSError:
        nodes_stamp = (0, 0)
    try:
        est = os.stat(_edges_file(root))
        edges_stamp = (int(est.st_mtime_ns), int(est.st_size))
    except OSError:
        edges_stamp = (0, 0)
    return (nodes_stamp[0], nodes_stamp[1], edges_stamp[0], edges_stamp[1])


def _cached_graph(root: str, as_of: str | None = None) -> tuple:
    """Load nodes/edges and build adjacency, memoized per (root, stamp, as_of).

    Returns ``(nodes, active_edges, adjacency)`` with the same content and
    ordering as an uncached load; ``as_of`` filtering is applied before the
    adjacency is built so cached results match fresh traversal exactly.
    """
    stamp = _graph_files_stamp(root)
    key = (os.path.abspath(str(root)), stamp, as_of or "")
    hit = _GRAPH_ADJ_CACHE.get(key)
    if hit is not None:
        return hit

    nodes = load_nodes(root)
    edges = load_edges(root)
    moment = lesson_cache.parse_moment(as_of) if as_of else None
    active_edges = [e for e in edges if _is_active_edge(e, moment)]

    adj: dict[str, list[GraphEdge]] = {}
    for e in active_edges:
        adj.setdefault(e.source, []).append(e)
        adj.setdefault(e.target, []).append(e)

    value = (nodes, active_edges, adj)
    _GRAPH_ADJ_CACHE[key] = value
    while len(_GRAPH_ADJ_CACHE) > _GRAPH_ADJ_CACHE_MAX_ENTRIES:
        oldest = next(iter(_GRAPH_ADJ_CACHE))
        if oldest == key:
            break
        del _GRAPH_ADJ_CACHE[oldest]
    return value


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
    provenance: dict[str, Any] | None = None,
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
    if provenance is not None:
        try:
            from commontrace import provenance as _prov

            _prov.append_provenance(
                root,
                target_kind="node",
                target_id=clean_id,
                source_path=str((provenance or {}).get("source_path", "")),
                run_id=str((provenance or {}).get("run_id", "")),
                detail=(provenance or {}).get("detail", ""),
            )
        except Exception as exc:
            logging.getLogger(__name__).debug("Failed to record provenance for node %s: %s", clean_id, exc)
    return node


def add_edge(
    root: str,
    source: str,
    target: str,
    relation: str,
    weight: float = 1.0,
    valid_at: str | None = None,
    invalid_at: str | None = None,
    expired_at: str | None = None,
    properties: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    valid_from: str | None = None,
) -> GraphEdge:
    """Add a directed relationship between two nodes with bi-temporal support.

    Bi-temporal fields:
    - valid_at: When the fact becomes true (transaction time)
    - invalid_at: When the fact becomes false (transaction time)
    - expired_at: When the record expires (validity time)

    Contradiction resolution: "latest valid_at wins" - if multiple edges exist
    between the same nodes with the same relation, the one with the latest
    valid_at timestamp is considered the current truth.
    """
    valid_at = valid_at or valid_from
    src = source.strip().lower()
    dst = target.strip().lower()
    if not src or not dst:
        raise ValueError("Source and target must be non-empty")
    try:
        from commontrace import ontology as _ontology_mod

        _ontology_present = _ontology_mod.ontology_exists(root)
    except Exception:
        _ontology_mod = None  # type: ignore[assignment]
        _ontology_present = False
    if _ontology_present and _ontology_mod is not None:
        try:
            canonical = _ontology_mod.validate_relation(root, relation)
            if not _ontology_mod.is_known_edge(root, relation):
                if canonical != relation:
                    warnings.warn(
                        f"graph.add_edge: unknown relation {relation!r}; "
                        f"falling back to {canonical!r}",
                        UserWarning,
                        stacklevel=2,
                    )
                relation = canonical
            else:
                relation = canonical
        except Exception:
            if relation not in RELATIONS:
                relation = "relates_to"
    elif relation not in RELATIONS:
        relation = "relates_to"

    nodes = load_nodes(root)
    if src not in nodes:
        add_node(root, src, entity_type="concept", provenance=provenance)
    if dst not in nodes:
        add_node(root, dst, entity_type="concept", provenance=provenance)

    edges = load_edges(root)
    now_iso = _now()
    valid_at = valid_at or now_iso

    # Contradiction resolution: "latest valid_at wins"
    # Find all edges between src, dst with same relation
    matching_edges = [
        e for e in edges
        if e.source == src and e.target == dst and e.relation == relation
    ]

    if matching_edges:
        # Find the edge with the latest valid_at
        latest_edge = max(
            matching_edges,
            key=lambda e: lesson_cache.parse_moment(e.valid_at or e.created_at)
        )
        latest_valid_at = lesson_cache.parse_moment(latest_edge.valid_at or latest_edge.created_at)
        new_valid_at = lesson_cache.parse_moment(valid_at)

        # If new edge has later valid_at, invalidate the old one
        if new_valid_at > latest_valid_at:
            for edge in matching_edges:
                if edge.invalid_at is None:
                    edge.invalid_at = valid_at
            # Create new edge with latest valid_at
            new_edge = GraphEdge(
                source=src,
                target=dst,
                relation=relation,
                weight=round(float(weight), 3),
                valid_at=valid_at,
                invalid_at=invalid_at,
                expired_at=expired_at,
                properties=properties or {},
                created_at=now_iso,
            )
            edges.append(new_edge)
            save_edges(root, edges)
        else:
            # New edge is older, just update the latest edge if needed
            latest_edge.weight = max(latest_edge.weight, float(weight))
            if properties:
                latest_edge.properties.update(properties)
            save_edges(root, edges)
            if provenance is not None:
                try:
                    from commontrace import provenance as _prov

                    _prov.append_provenance(
                        root,
                        target_kind="edge",
                        target_id=f"{src}->{dst}:{relation}",
                        source_path=str((provenance or {}).get("source_path", "")),
                        run_id=str((provenance or {}).get("run_id", "")),
                        detail=(provenance or {}).get("detail", ""),
                    )
                except Exception as exc:
                    logging.getLogger(__name__).debug("Failed to record provenance for updated edge: %s", exc)
            return latest_edge
    else:
        # No existing edge, create new one
        new_edge = GraphEdge(
            source=src,
            target=dst,
            relation=relation,
            weight=round(float(weight), 3),
            valid_at=valid_at,
            invalid_at=invalid_at,
            expired_at=expired_at,
            properties=properties or {},
            created_at=now_iso,
        )
        edges.append(new_edge)
        save_edges(root, edges)

    if provenance is not None:
        try:
            from commontrace import provenance as _prov

            _prov.append_provenance(
                root,
                target_kind="edge",
                target_id=f"{src}->{dst}:{relation}",
                source_path=str((provenance or {}).get("source_path", "")),
                run_id=str((provenance or {}).get("run_id", "")),
                detail=(provenance or {}).get("detail", ""),
            )
        except Exception as exc:
            logging.getLogger(__name__).debug("Failed to record provenance for new edge: %s", exc)
    return new_edge


def _is_active_edge(edge: GraphEdge, moment: datetime | None) -> bool:
    """Check if an edge is active at a given moment using bi-temporal logic.

    An edge is active if:
    1. It has been validated (valid_at <= moment or valid_at is None)
    2. It has not been invalidated (invalid_at is None or invalid_at > moment)
    3. It has not expired (expired_at is None or expired_at > moment)

    If moment is None, return edges that are currently valid (not invalidated).
    """
    if not moment:
        # Current time: edge is active if not invalidated and not expired
        if edge.invalid_at is not None:
            return False
        if edge.expired_at is not None:
            try:
                exp = lesson_cache.parse_moment(edge.expired_at)
                now = lesson_cache.parse_moment(_now())
                if exp <= now:
                    return False
            except Exception:
                pass
        return True

    # Historical query: check all bi-temporal conditions
    if edge.valid_at:
        try:
            va = lesson_cache.parse_moment(edge.valid_at)
            if va > moment:
                return False
        except Exception:
            pass

    if edge.invalid_at:
        try:
            ia = lesson_cache.parse_moment(edge.invalid_at)
            if ia <= moment:
                return False
        except Exception:
            pass

    if edge.expired_at:
        try:
            exp = lesson_cache.parse_moment(edge.expired_at)
            if exp <= moment:
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
    max_edges: int | None = None,
) -> dict[str, Any]:
    """Traverse graph up to max_hops from the given start nodes.

    ``max_edges`` caps the total collected edges (``None`` = uncapped, which
    preserves the historical behavior exactly). Traversal stops early once the
    cap is reached.
    """
    nodes, active_edges, adj = _cached_graph(root, as_of)
    _ = active_edges  # adjacency already reflects the active edge set.

    cap: int | None = None if max_edges is None else max(0, int(max_edges))

    visited_nodes: dict[str, int] = {}  # node_id -> hop_distance
    collected_edges: list[GraphEdge] = []
    queue: list[tuple[str, int]] = []

    for sid in start_node_ids:
        cid = sid.strip().lower()
        if cid in nodes:
            visited_nodes[cid] = 0
            queue.append((cid, 0))

    while queue:
        if cap is not None and len(collected_edges) >= cap:
            break
        curr_id, hop = queue.pop(0)
        if hop >= max_hops:
            continue

        for edge in adj.get(curr_id, []):
            if cap is not None and len(collected_edges) >= cap:
                break
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
    max_hops: int = 2,
    max_edges: int | None = None,
) -> dict[str, float]:
    """Compute graph-proximity boosts for candidate lesson slugs.

    ``max_hops`` defaults to 2 (the historical traversal depth) and
    ``max_edges`` defaults to ``None`` (uncapped); both are passed through to
    :func:`multi_hop_subgraph`, so default calls behave exactly as before.
    """
    entities = extract_entities_from_text(root, query)
    if not entities or not candidate_slugs:
        return {s: 0.0 for s in candidate_slugs}

    subgraph = multi_hop_subgraph(
        root, start_node_ids=entities, max_hops=max_hops, as_of=as_of, max_edges=max_edges
    )
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


def query_edges_by_interval(
    root: str,
    starts_at: str | None = None,
    ends_at: str | None = None,
    source: str | None = None,
    target: str | None = None,
    relation: str | None = None,
) -> list[GraphEdge]:
    """Temporal retriever with interval queries.

    Returns edges that were active at any point within the time interval
    [starts_at, ends_at]. Handles corrupted dates and bounds robustly.
    """
    edges = load_edges(root)
    utc = timezone.utc

    def _safe_moment(val: Any) -> datetime | None:
        if not val:
            return None
        try:
            return lesson_cache.parse_moment(val)
        except (ValueError, TypeError):
            return None

    start_moment = _safe_moment(starts_at)
    end_moment = _safe_moment(ends_at)

    clean_source = source.strip().lower() if source else None
    clean_target = target.strip().lower() if target else None

    results: list[GraphEdge] = []

    for edge in edges:
        # Apply node and relation filters
        if clean_source and edge.source.strip().lower() != clean_source:
            continue
        if clean_target and edge.target.strip().lower() != clean_target:
            continue
        if relation and edge.relation != relation:
            continue

        edge_valid_at = _safe_moment(edge.valid_at)
        edge_invalid_at = _safe_moment(edge.invalid_at)
        edge_expired_at = _safe_moment(edge.expired_at)

        # Active window start: default to -inf if unspecified
        edge_start = edge_valid_at or datetime.min.replace(tzinfo=utc)

        # Active window end: earliest of invalid_at or expired_at
        end_candidates = [m for m in (edge_invalid_at, edge_expired_at) if m is not None]
        if end_candidates:
            edge_end = min(end_candidates)
        else:
            edge_end = datetime.max.replace(tzinfo=utc)

        query_start = start_moment or datetime.min.replace(tzinfo=utc)
        query_end = end_moment or datetime.max.replace(tzinfo=utc)

        # Intervals overlap if: edge_start <= query_end AND edge_end >= query_start
        if edge_start <= query_end and edge_end >= query_start:
            results.append(edge)

    return results


# ---------------------------------------------------------------------------
# Version chain management for memory evolution (Supermemory pattern)
# ---------------------------------------------------------------------------


def get_version_chain(
    root: str,
    node_id: str,
) -> list[GraphNode]:
    """Get the complete version chain for a memory node.

    Returns all versions of a memory from root to latest, ordered by version number.
    This enables tracking memory evolution over time.

    Args:
        root: Graph store root directory
        node_id: The node ID to get the version chain for

    Returns:
        List of GraphNode objects representing the version chain, ordered by version
    """
    nodes = load_nodes(root)
    target_node = nodes.get(node_id.strip().lower())

    if not target_node:
        return []

    # Find the root of the version chain
    root_id = target_node.root_id or target_node.id

    # Collect all nodes in the version chain
    chain_nodes: list[GraphNode] = []
    for node in nodes.values():
        if node.root_id == root_id or node.id == root_id:
            chain_nodes.append(node)

    # Sort by version number
    chain_nodes.sort(key=lambda n: n.version)

    return chain_nodes


def create_memory_version(
    root: str,
    parent_id: str,
    new_properties: dict[str, Any] | None = None,
    new_name: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> GraphNode:
    """Create a new version of a memory node.

    Establishes version chain with parent node, incrementing version number.
    The parent node's is_latest flag is set to False.

    Args:
        root: Graph store root directory
        parent_id: ID of the parent memory node
        new_properties: New properties for the version (merged with parent)
        new_name: New name for the version (optional)
        provenance: Optional provenance metadata

    Returns:
        The newly created version node
    """
    nodes = load_nodes(root)
    parent = nodes.get(parent_id.strip().lower())

    if not parent:
        raise ValueError(f"Parent node {parent_id} not found")

    # Determine root_id
    root_id = parent.root_id or parent.id

    # Find the next version number
    chain = get_version_chain(root, parent_id)
    next_version = max((n.version for n in chain), default=0) + 1

    # Merge properties
    merged_properties = dict(parent.properties)
    if new_properties:
        merged_properties.update(new_properties)

    # Create new version node
    new_id = f"{root_id}:v{next_version}"
    now_iso = _now()

    new_node = GraphNode(
        id=new_id,
        entity_type=parent.entity_type,
        name=new_name or parent.name,
        properties=merged_properties,
        created_at=now_iso,
        updated_at=now_iso,
        parent_id=parent.id,
        root_id=root_id,
        version=next_version,
        is_latest=True,
        is_forgotten=False,
    )

    # Update parent's is_latest flag
    parent.is_latest = False
    parent.updated_at = now_iso

    # Save both nodes
    nodes[parent.id] = parent
    nodes[new_id] = new_node
    save_nodes(root, nodes)

    # Record provenance
    if provenance is not None:
        try:
            from commontrace import provenance as _prov

            _prov.append_provenance(
                root,
                target_kind="node",
                target_id=new_id,
                source_path=str((provenance or {}).get("source_path", "")),
                run_id=str((provenance or {}).get("run_id", "")),
                detail=(provenance or {}).get("detail", ""),
            )
        except Exception as exc:
            logging.getLogger(__name__).debug("Failed to record provenance for version node %s: %s", new_id, exc)

    return new_node


def get_memory_relationships(
    root: str,
    node_id: str,
) -> dict[str, str]:
    """Get all memory relationships for a node.

    Returns a mapping of relationship type to target node ID for memory-specific
    relationships (updates, extends, derives).

    Args:
        root: Graph store root directory
        node_id: The node ID to get relationships for

    Returns:
        Dict mapping relationship type to target node ID
    """
    edges = load_edges(root)
    relationships: dict[str, str] = {}

    memory_relations = {"updates", "extends", "derives"}

    for edge in edges:
        if edge.source == node_id.strip().lower() and edge.relation in memory_relations:
            if _is_active_edge(edge, None):
                relationships[edge.relation] = edge.target

    return relationships


def add_memory_relationship(
    root: str,
    source_id: str,
    target_id: str,
    relation: str,
    provenance: dict[str, Any] | None = None,
) -> GraphEdge:
    """Add a memory-specific relationship between nodes.

    Convenience function for adding memory relationships (updates, extends, derives).
    Uses the standard add_edge but validates the relation type.

    Args:
        root: Graph store root directory
        source_id: Source node ID
        target_id: Target node ID
        relation: Relationship type (must be one of: updates, extends, derives)
        provenance: Optional provenance metadata

    Returns:
        The created edge
    """
    valid_relations = {"updates", "extends", "derives"}

    if relation not in valid_relations:
        raise ValueError(
            f"Invalid memory relation '{relation}'. "
            f"Must be one of: {', '.join(sorted(valid_relations))}"
        )

    return add_edge(
        root,
        source=source_id,
        target=target_id,
        relation=relation,
        weight=1.0,
        provenance=provenance,
    )


def resolve_contradictions(
    edges: list[GraphEdge],
    as_of: str | None = None,
) -> list[GraphEdge]:
    """Apply 'latest valid_at wins' policy for conflicting edges (Zep/Graphiti pattern).

    Groups edges by (source, target, relation). For each conflicting group,
    selects the edge with the latest valid_at timestamp active as of `as_of`.
    If valid_at ties, the edge with the latest created_at wins.
    """
    utc = timezone.utc
    moment: datetime | None = None
    if as_of:
        try:
            moment = lesson_cache.parse_moment(as_of)
        except (ValueError, TypeError):
            moment = None

    active = [e for e in edges if _is_active_edge(e, moment)]

    grouped: dict[tuple[str, str, str], list[GraphEdge]] = {}
    for edge in active:
        key = (edge.source.strip().lower(), edge.target.strip().lower(), edge.relation)
        grouped.setdefault(key, []).append(edge)

    def _safe_sort_key(edge: GraphEdge) -> tuple[datetime, datetime]:
        min_dt = datetime.min.replace(tzinfo=utc)
        v_dt = min_dt
        c_dt = min_dt
        if edge.valid_at:
            try:
                v_dt = lesson_cache.parse_moment(edge.valid_at)
            except Exception:
                pass
        if edge.created_at:
            try:
                c_dt = lesson_cache.parse_moment(edge.created_at)
            except Exception:
                pass
        return (v_dt or c_dt, c_dt)

    resolved: list[GraphEdge] = []
    for _key, group in grouped.items():
        if len(group) == 1:
            resolved.append(group[0])
        else:
            winner = max(group, key=_safe_sort_key)
            resolved.append(winner)

    return resolved


def forget_node(
    root: str,
    node_id: str,
    reason: str = "superseded",
    invalidate_edges: bool = True,
    undo: bool = False,
    provenance: dict[str, Any] | None = None,
) -> GraphNode | None:
    """Soft-delete/forget a memory node (Supermemory forgetting pattern).

    Marks the node as forgotten (or restores if undo=True), sets forget reason,
    optionally invalidates connected active edges, and invalidates graph cache.
    """
    nodes = load_nodes(root)
    clean_id = node_id.strip().lower()
    node = nodes.get(clean_id)
    if not node:
        return None

    now_iso = _now()
    node.is_forgotten = not undo
    node.properties["is_forgotten"] = not undo
    if not undo:
        node.properties["forget_reason"] = reason
        node.properties["forgotten_at"] = now_iso
    else:
        node.properties.pop("forget_reason", None)
        node.properties.pop("forgotten_at", None)
        node.properties["restored_at"] = now_iso

    node.updated_at = now_iso
    save_nodes(root, nodes)

    if invalidate_edges and not undo:
        edges = load_edges(root)
        modified = False
        for edge in edges:
            if edge.source == clean_id or edge.target == clean_id:
                if edge.invalid_at is None:
                    edge.invalid_at = now_iso
                    modified = True
        if modified:
            save_edges(root, edges)

    _clear_graph_cache()

    if provenance is not None:
        try:
            from commontrace import provenance as _prov
            _prov.append_provenance(
                root,
                target_kind="node",
                target_id=clean_id,
                source_path=str((provenance or {}).get("source_path", "")),
                run_id=str((provenance or {}).get("run_id", "")),
                detail=f"node {'restored' if undo else 'forgotten'}: {reason}",
            )
        except Exception as exc:
            logging.getLogger(__name__).debug("Failed to record provenance for forgotten node %s: %s", clean_id, exc)

    return node


def list_version_chains(root: str) -> dict[str, list[GraphNode]]:
    """List all version chains in the graph, grouped by root_id.

    Returns:
        Dict mapping root_id to sorted list of GraphNodes in that chain.
    """
    nodes = load_nodes(root)
    chains: dict[str, list[GraphNode]] = {}
    for node in nodes.values():
        root_key = node.root_id or node.id
        chains.setdefault(root_key, []).append(node)

    for root_key in chains:
        chains[root_key].sort(key=lambda n: n.version)

    return chains

