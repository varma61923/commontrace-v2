"""Temporal knowledge graph: typed nodes, bitemporal edges, multi-hop traversal."""
from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
from collections import deque
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from commontrace import _jsonl, lesson_cache, paths

ENTITY_TYPES = (
    "service", "tool", "error", "concept", "lesson", "scope", "user", "file",
    "symbol", "memory", "document",
)
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
    "contains",
    "mentions",
    "raises",
    "updates",
    "extends",
    "derives",
)
FALLBACK_RELATION = "relates_to"
MAX_HOPS = 4

_log = logging.getLogger(__name__)


@dataclass
class GraphNode:
    id: str
    entity_type: str
    name: str
    properties: dict[str, Any]
    created_at: str
    updated_at: str
    parent_id: str | None = None
    root_id: str | None = None
    version: int = 1
    is_latest: bool = True
    is_forgotten: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphEdge:
    source: str
    target: str
    relation: str
    weight: float
    valid_at: str | None
    invalid_at: str | None
    expired_at: str | None
    properties: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _graph_dir(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "graph")


def _nodes_file(root: str) -> str:
    return os.path.join(_graph_dir(root), "nodes.jsonl")


def _edges_file(root: str) -> str:
    return os.path.join(_graph_dir(root), "edges.jsonl")


def _lock_file(root: str) -> str:
    return os.path.join(_graph_dir(root), "graph")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_id(value: str) -> str:
    return str(value or "").strip().lower()


def _clamp_weight(weight: float) -> float:
    try:
        value = float(weight)
    except (TypeError, ValueError):
        value = 1.0
    if value != value:
        value = 1.0
    return round(min(1.0, max(0.0, value)), 3)


def _moment_or_none(value: str | None, label: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return lesson_cache.parse_moment(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid edge `{label}` value {value!r}: {exc}") from exc


def load_nodes(root: str) -> dict[str, GraphNode]:
    nodes: dict[str, GraphNode] = {}
    for row in _jsonl.read_rows(_nodes_file(root)):
        try:
            node = GraphNode(**row)
        except TypeError:
            continue
        nodes[node.id] = node
    return nodes


def load_edges(root: str) -> list[GraphEdge]:
    edges: list[GraphEdge] = []
    for row in _jsonl.read_rows(_edges_file(root)):
        try:
            edges.append(GraphEdge(**row))
        except TypeError:
            continue
    return edges


def save_nodes(root: str, nodes: dict[str, GraphNode]) -> None:
    _jsonl.write_rows(_nodes_file(root), (n.to_dict() for n in nodes.values()))


def save_edges(root: str, edges: list[GraphEdge]) -> None:
    _jsonl.write_rows(_edges_file(root), (e.to_dict() for e in edges))


class _Txn:
    def __init__(self, root: str) -> None:
        self.root = root
        self.nodes = load_nodes(root)
        self.edges = load_edges(root)
        self.by_key: dict[tuple[str, str, str], list[GraphEdge]] = {}
        for edge in self.edges:
            self.by_key.setdefault((edge.source, edge.target, edge.relation), []).append(edge)
        self.nodes_dirty = False
        self.edges_dirty = False
        self.provenance: list[dict[str, Any]] = []

    def flush(self) -> None:
        if self.nodes_dirty:
            save_nodes(self.root, self.nodes)
        if self.edges_dirty:
            save_edges(self.root, self.edges)
        if self.nodes_dirty or self.edges_dirty:
            _clear_graph_cache()
        if self.provenance:
            from commontrace import provenance as _prov

            for rec in self.provenance:
                try:
                    _prov.append_provenance(self.root, **rec)
                except OSError as exc:
                    _log.debug("could not record provenance for %s: %s", rec.get("target_id"), exc)


_local = threading.local()


@contextlib.contextmanager
def batch(root: str) -> Iterator[_Txn]:
    """Group many graph writes into one locked load and one save."""
    key = os.path.abspath(root)
    open_txns: dict[str, _Txn] = getattr(_local, "txns", None) or {}
    _local.txns = open_txns
    if key in open_txns:
        yield open_txns[key]
        return
    with _jsonl.locked(_lock_file(root)):
        txn = _Txn(root)
        open_txns[key] = txn
        try:
            yield txn
        finally:
            del open_txns[key]
        txn.flush()


def _provenance(txn: _Txn, kind: str, target_id: str, provenance: dict[str, Any] | None, detail=None) -> None:
    if provenance is None:
        return
    txn.provenance.append({
        "target_kind": kind,
        "target_id": target_id,
        "source_path": str(provenance.get("source_path", "")),
        "run_id": str(provenance.get("run_id", "")),
        "detail": provenance.get("detail", "") if detail is None else detail,
    })


def _put_node(
    txn: _Txn, node_id: str, entity_type: str, name: str, properties: dict[str, Any] | None,
    provenance: dict[str, Any] | None,
) -> GraphNode:
    clean_id = _clean_id(node_id)
    if not clean_id:
        raise ValueError("Node ID cannot be empty")
    if entity_type not in ENTITY_TYPES:
        entity_type = "concept"
    now_iso = _now()
    node = txn.nodes.get(clean_id)
    if node is not None:
        changed = False
        if name and name.strip() != node.name:
            node.name = name.strip()
            changed = True
        if properties:
            merged = {**node.properties, **properties}
            if merged != node.properties:
                node.properties = merged
                changed = True
        if changed:
            node.updated_at = now_iso
            txn.nodes_dirty = True
    else:
        node = GraphNode(
            id=clean_id,
            entity_type=entity_type,
            name=(name or "").strip() or clean_id,
            properties=dict(properties or {}),
            created_at=now_iso,
            updated_at=now_iso,
        )
        txn.nodes[clean_id] = node
        txn.nodes_dirty = True
    _provenance(txn, "node", clean_id, provenance)
    return node


def add_node(
    root: str,
    id: str,
    entity_type: str,
    name: str = "",
    properties: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> GraphNode:
    """Add a node, or update the name and properties of an existing one."""
    with batch(root) as txn:
        return _put_node(txn, id, entity_type, name, properties, provenance)


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
    """Add a directed edge; the latest `valid_at` wins between equal edges."""
    src, dst = _clean_id(source), _clean_id(target)
    if not src or not dst:
        raise ValueError("Source and target must be non-empty")
    if relation not in RELATIONS:
        relation = FALLBACK_RELATION
    now_iso = _now()
    explicit_valid_at = _moment_or_none(valid_at or valid_from, "valid_at")
    valid_at = explicit_valid_at or now_iso
    invalid_at = _moment_or_none(invalid_at, "invalid_at")
    expired_at = _moment_or_none(expired_at, "expired_at")
    if invalid_at and lesson_cache.parse_moment(invalid_at) <= lesson_cache.parse_moment(valid_at):
        raise ValueError(f"edge `invalid_at` ({invalid_at}) must be after `valid_at` ({valid_at})")
    weight = _clamp_weight(weight)

    with batch(root) as txn:
        for node_id in (src, dst):
            if node_id not in txn.nodes:
                _put_node(txn, node_id, "concept", "", None, provenance)
        key = (src, dst, relation)
        matching = txn.by_key.get(key, [])
        edge_id = f"{src}->{dst}:{relation}"
        if matching:
            latest = max(matching, key=lambda e: lesson_cache.parse_moment(e.valid_at or e.created_at))
            restated = explicit_valid_at is None and latest.invalid_at is None and invalid_at is None
            if restated or lesson_cache.parse_moment(valid_at) <= lesson_cache.parse_moment(
                    latest.valid_at or latest.created_at):
                if weight > latest.weight:
                    latest.weight = weight
                    txn.edges_dirty = True
                if properties:
                    merged = {**latest.properties, **properties}
                    if merged != latest.properties:
                        latest.properties = merged
                        txn.edges_dirty = True
                _provenance(txn, "edge", edge_id, provenance)
                return latest
            for edge in matching:
                if edge.invalid_at is None:
                    edge.invalid_at = valid_at
        new_edge = GraphEdge(
            source=src,
            target=dst,
            relation=relation,
            weight=weight,
            valid_at=valid_at,
            invalid_at=invalid_at,
            expired_at=expired_at,
            properties=dict(properties or {}),
            created_at=now_iso,
        )
        txn.edges.append(new_edge)
        txn.by_key.setdefault(key, []).append(new_edge)
        txn.edges_dirty = True
        _provenance(txn, "edge", edge_id, provenance)
        return new_edge


def _is_active_edge(edge: GraphEdge, moment: datetime | None) -> bool:
    def _at(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return lesson_cache.parse_moment(value)
        except ValueError:
            return None

    if moment is None:
        if edge.invalid_at is not None:
            return False
        expires = _at(edge.expired_at)
        return expires is None or expires > datetime.now(timezone.utc)
    start = _at(edge.valid_at)
    if start is not None and start > moment:
        return False
    for end in (_at(edge.invalid_at), _at(edge.expired_at)):
        if end is not None and end <= moment:
            return False
    return True


_GRAPH_ADJ_CACHE: dict[tuple, tuple] = {}
_GRAPH_ADJ_CACHE_MAX_ENTRIES = 64


def _clear_graph_cache() -> None:
    _GRAPH_ADJ_CACHE.clear()
    _PHRASE_INDEX.clear()
    _SCANNED_ONCE.clear()


def _graph_files_stamp(root: str) -> tuple[int, int, int, int]:
    stamps: list[int] = []
    for path in (_nodes_file(root), _edges_file(root)):
        try:
            st = os.stat(path)
            stamps.extend((int(st.st_mtime_ns), int(st.st_size)))
        except OSError:
            stamps.extend((0, 0))
    return tuple(stamps)  # type: ignore[return-value]


def _cached_graph(root: str, as_of: str | None = None) -> tuple:
    key = (os.path.abspath(str(root)), _graph_files_stamp(root), as_of or "")
    hit = _GRAPH_ADJ_CACHE.get(key)
    if hit is not None:
        return hit
    nodes = load_nodes(root)
    moment = lesson_cache.parse_moment(as_of) if as_of else None
    active = [e for e in load_edges(root) if _is_active_edge(e, moment)]
    adj: dict[str, list[GraphEdge]] = {}
    for e in active:
        adj.setdefault(e.source, []).append(e)
        if e.target != e.source:
            adj.setdefault(e.target, []).append(e)
    value = (nodes, active, adj)
    if len(_GRAPH_ADJ_CACHE) >= _GRAPH_ADJ_CACHE_MAX_ENTRIES:
        _GRAPH_ADJ_CACHE.pop(next(iter(_GRAPH_ADJ_CACHE)))
    _GRAPH_ADJ_CACHE[key] = value
    return value


def get_neighbors(
    root: str,
    node_id: str,
    direction: str = "both",
    relation: str | None = None,
    as_of: str | None = None,
) -> list[dict[str, Any]]:
    """Nodes one edge away from *node_id*, with the connecting relation."""
    clean_id = _clean_id(node_id)
    nodes, _active, adj = _cached_graph(root, as_of)
    results: list[dict[str, Any]] = []
    for edge in adj.get(clean_id, []):
        if relation and edge.relation != relation:
            continue
        for side, other in (("out", edge.target), ("in", edge.source)):
            if direction not in (side, "both"):
                continue
            if (side == "out" and edge.source != clean_id) or (side == "in" and edge.target != clean_id):
                continue
            other_node = nodes.get(other)
            results.append({
                "neighbor_id": other,
                "neighbor_name": other_node.name if other_node else other,
                "neighbor_type": other_node.entity_type if other_node else "concept",
                "direction": side,
                "relation": edge.relation,
                "weight": edge.weight,
            })
            break
    return results


def multi_hop_subgraph(
    root: str,
    start_node_ids: list[str],
    max_hops: int = 2,
    as_of: str | None = None,
    max_edges: int | None = None,
) -> dict[str, Any]:
    """Breadth-first subgraph within *max_hops* of the start nodes."""
    nodes, _active, adj = _cached_graph(root, as_of)
    hops_limit = max(0, min(int(max_hops), MAX_HOPS))
    cap = None if max_edges is None else max(0, int(max_edges))
    visited: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque()
    for sid in start_node_ids:
        cid = _clean_id(sid)
        node = nodes.get(cid)
        if node is not None and not node.is_forgotten and cid not in visited:
            visited[cid] = 0
            queue.append((cid, 0))
    collected: list[GraphEdge] = []
    seen_edges: set[int] = set()
    while queue and (cap is None or len(collected) < cap):
        current, hop = queue.popleft()
        if hop >= hops_limit:
            continue
        for edge in adj.get(current, []):
            if cap is not None and len(collected) >= cap:
                break
            if id(edge) not in seen_edges:
                seen_edges.add(id(edge))
                collected.append(edge)
            neighbor = edge.target if edge.source == current else edge.source
            if neighbor not in visited:
                visited[neighbor] = hop + 1
                queue.append((neighbor, hop + 1))
    return {
        "nodes": [nodes[nid].to_dict() for nid in visited if nid in nodes],
        "edges": [e.to_dict() for e in collected],
        "hop_distances": visited,
    }


_WORD_RE = re.compile(r"[a-z0-9]+(?:[_.\-][a-z0-9]+)*")


_PHRASE_INDEX: dict[tuple, dict[str, list[tuple[tuple[str, ...], str]]]] = {}


def _node_terms(nid: str, node: GraphNode) -> list[tuple[str, ...]]:
    name = node.name.lower()
    terms = [name, nid.split(":", 1)[-1]]
    if "::" in name:
        leaf = name.rsplit("::", 1)[-1]
        terms += [leaf, leaf.rsplit(".", 1)[-1]]
    out = []
    for term in terms:
        words = tuple(_WORD_RE.findall(term))
        if words and len(" ".join(words)) >= 2:
            out.append(words)
    return out


def _phrase_index(root: str) -> dict[str, list[tuple[tuple[str, ...], str]]]:
    """First word -> [(phrase words, node id)] for every live node, cached per graph version."""
    key = (os.path.abspath(str(root)), _graph_files_stamp(root))
    hit = _PHRASE_INDEX.get(key)
    if hit is not None:
        return hit
    nodes, _active, _adj = _cached_graph(root)
    index: dict[str, list[tuple[tuple[str, ...], str]]] = {}
    for nid, node in nodes.items():
        if node.is_forgotten:
            continue
        for words in set(_node_terms(nid, node)):
            index.setdefault(words[0], []).append((words, nid))
    if len(_PHRASE_INDEX) >= _GRAPH_ADJ_CACHE_MAX_ENTRIES:
        _PHRASE_INDEX.pop(next(iter(_PHRASE_INDEX)))
    _PHRASE_INDEX[key] = index
    return index


_SCANNED_ONCE: set[tuple] = set()


def extract_entities_from_text(root: str, text: str) -> list[str]:
    """Ids of live nodes whose name or bare id appears as a whole term in *text*.

    The first lookup for a graph version scans the nodes directly; a second
    one builds a phrase index, so a one-shot CLI call never pays to build it.
    """
    if not text or not text.strip():
        return []
    words = _WORD_RE.findall(text.lower())
    if not words:
        return []
    key = (os.path.abspath(str(root)), _graph_files_stamp(root))
    if key not in _PHRASE_INDEX and key not in _SCANNED_ONCE:
        _SCANNED_ONCE.add(key)
        nodes, _active, _adj = _cached_graph(root)
        norm = " " + " ".join(words) + " "
        return sorted(
            nid for nid, node in nodes.items()
            if not node.is_forgotten and any(f" {' '.join(t)} " in norm for t in _node_terms(nid, node))
        )
    index = _phrase_index(root)
    matched: set[str] = set()
    for i, word in enumerate(words):
        for phrase, nid in index.get(word, ()):
            if tuple(words[i:i + len(phrase)]) == phrase:
                matched.add(nid)
    return sorted(matched)


def graph_boost_for_lessons(
    root: str,
    query: str,
    candidate_slugs: list[str],
    as_of: str | None = None,
    max_hops: int = 2,
    max_edges: int | None = None,
) -> dict[str, float]:
    """Proximity boost per lesson slug: 0.25 / (1 + hops) from entities in *query*."""
    boosts = dict.fromkeys(candidate_slugs, 0.0)
    if not candidate_slugs or not os.path.exists(_nodes_file(root)):
        return boosts
    entities = extract_entities_from_text(root, query)
    if not entities:
        return boosts
    hop_distances = multi_hop_subgraph(
        root, start_node_ids=entities, max_hops=max_hops, as_of=as_of, max_edges=max_edges,
    ).get("hop_distances", {})
    for slug in candidate_slugs:
        hop = hop_distances.get(f"lesson:{slug}")
        if hop is not None:
            boosts[slug] = round(0.25 / (1 + hop), 3)
    return boosts


def _mermaid_label(text: str) -> str:
    return (
        str(text).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
        .replace(">", "&gt;").replace("[", "&#91;").replace("]", "&#93;")
        .replace("|", "&#124;").replace("\n", " ")
    )


def _mermaid_id(node_id: str) -> str:
    return "n_" + re.sub(r"[^a-zA-Z0-9_]", "_", node_id)


def export_mermaid(root: str, as_of: str | None = None) -> str:
    """The graph as a Mermaid flowchart, with labels escaped."""
    nodes, active, _adj = _cached_graph(root, as_of)
    if not nodes and not active:
        return "graph TD\n  empty[Knowledge graph is empty]"
    lines = ["graph TD"]
    ids: dict[str, str] = {}
    for nid in sorted(nodes):
        node = nodes[nid]
        if node.is_forgotten:
            continue
        mid = _mermaid_id(nid)
        while mid in ids.values():
            mid += "_"
        ids[nid] = mid
        lines.append(f'  {mid}["{_mermaid_label(node.name)} ({_mermaid_label(node.entity_type)})"]')
    for edge in active:
        src = ids.get(edge.source) or _mermaid_id(edge.source)
        dst = ids.get(edge.target) or _mermaid_id(edge.target)
        lines.append(f"  {src} -->|{_mermaid_label(edge.relation)}| {dst}")
    return "\n".join(lines)


def export_json(root: str, as_of: str | None = None) -> dict[str, Any]:
    """The graph as JSON: every node, and the edges active at *as_of*."""
    nodes, active, _adj = _cached_graph(root, as_of)
    return {
        "nodes": [n.to_dict() for n in nodes.values()],
        "edges": [e.to_dict() for e in active],
        "total_nodes": len(nodes),
        "total_active_edges": len(active),
    }


def get_version_chain(root: str, node_id: str) -> list[GraphNode]:
    """Every version of *node_id*'s entity, oldest first."""
    nodes = load_nodes(root)
    target = nodes.get(_clean_id(node_id))
    if target is None:
        return []
    root_id = target.root_id or target.id
    chain = [n for n in nodes.values() if n.root_id == root_id or n.id == root_id]
    return sorted(chain, key=lambda n: n.version)


def list_version_chains(root: str) -> dict[str, list[GraphNode]]:
    """Nodes grouped by version-chain root, each chain oldest first."""
    chains: dict[str, list[GraphNode]] = {}
    for node in load_nodes(root).values():
        chains.setdefault(node.root_id or node.id, []).append(node)
    for chain in chains.values():
        chain.sort(key=lambda n: n.version)
    return chains


def forget_node(
    root: str,
    node_id: str,
    reason: str = "superseded",
    invalidate_edges: bool = True,
    undo: bool = False,
    provenance: dict[str, Any] | None = None,
) -> GraphNode | None:
    """Forget a node (and close its live edges), or restore it with ``undo=True``."""
    clean_id = _clean_id(node_id)
    with batch(root) as txn:
        node = txn.nodes.get(clean_id)
        if node is None:
            return None
        now_iso = _now()
        forgotten_at = node.properties.get("forgotten_at")
        node.is_forgotten = not undo
        node.properties["is_forgotten"] = not undo
        if undo:
            node.properties.pop("forget_reason", None)
            node.properties.pop("forgotten_at", None)
            node.properties["restored_at"] = now_iso
        else:
            node.properties["forget_reason"] = reason
            node.properties["forgotten_at"] = now_iso
        node.updated_at = now_iso
        txn.nodes_dirty = True
        for edge in txn.edges:
            if clean_id not in (edge.source, edge.target):
                continue
            if undo and forgotten_at and edge.invalid_at == forgotten_at:
                edge.invalid_at = None
                txn.edges_dirty = True
            elif not undo and invalidate_edges and edge.invalid_at is None:
                edge.invalid_at = now_iso
                txn.edges_dirty = True
        _provenance(txn, "node", clean_id, provenance,
                    detail=f"node {'restored' if undo else 'forgotten'}: {reason}")
        return node


def create_memory_version(
    root: str,
    parent_id: str,
    new_properties: dict[str, Any] | None = None,
    new_name: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> GraphNode:
    """Add the next version of a node; the previous version stops being the latest."""
    with batch(root) as txn:
        parent = txn.nodes.get(_clean_id(parent_id))
        if parent is None:
            raise ValueError(f"Parent node {parent_id} not found")
        root_id = parent.root_id or parent.id
        chain = [n for n in txn.nodes.values() if n.root_id == root_id or n.id == root_id]
        version = max((n.version for n in chain), default=0) + 1
        new_id = f"{root_id}:v{version}"
        while new_id in txn.nodes:
            version += 1
            new_id = f"{root_id}:v{version}"
        now_iso = _now()
        node = GraphNode(
            id=new_id,
            entity_type=parent.entity_type,
            name=(new_name or "").strip() or parent.name,
            properties={**parent.properties, **(new_properties or {})},
            created_at=now_iso,
            updated_at=now_iso,
            parent_id=parent.id,
            root_id=root_id,
            version=version,
        )
        for member in chain:
            if member.is_latest:
                member.is_latest = False
                member.updated_at = now_iso
        txn.nodes[new_id] = node
        txn.nodes_dirty = True
        _provenance(txn, "node", new_id, provenance)
    add_edge(root, new_id, parent.id, "updates", provenance=provenance)
    return node
