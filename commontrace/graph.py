"""Temporal knowledge graph: typed nodes, bitemporal edges, multi-hop traversal."""
from __future__ import annotations

import bisect
import contextlib
import copy
import errno
import logging
import os
import re
import threading
from collections import deque
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import cached_property
from typing import Any

from commontrace import _jsonl, lesson_cache, paths

ENTITY_TYPES = (
    "service", "tool", "error", "concept", "lesson", "scope", "user", "file",
    "symbol", "memory", "document", "person", "place", "organization", "event",
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
        self.by_source_relation: dict[tuple[str, str], list[GraphEdge]] = {}
        self.by_entity: dict[str, list[GraphEdge]] = {}
        for edge in self.edges:
            self.by_key.setdefault((edge.source, edge.target, edge.relation), []).append(edge)
            self.by_source_relation.setdefault((edge.source, edge.relation), []).append(edge)
            self.by_entity.setdefault(edge.source, []).append(edge)
            if edge.target != edge.source:
                self.by_entity.setdefault(edge.target, []).append(edge)
        from commontrace import ontology

        self.onto = ontology.load(root)
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
    clean_id = _clean_id(txn.onto.canonical_id(_clean_id(node_id)))
    if not clean_id:
        raise ValueError("Node ID cannot be empty")
    entity_type = txn.onto.entity_type(entity_type)
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
    """Add a directed edge; the latest `valid_at` wins between equal edges, and for an
    exclusive relation (one value at a time) between edges from the same source."""
    src, dst = _clean_id(source), _clean_id(target)
    if not src or not dst:
        raise ValueError("Source and target must be non-empty")
    now_iso = _now()
    explicit_valid_at = _moment_or_none(valid_at or valid_from, "valid_at")
    valid_at = explicit_valid_at or now_iso
    invalid_at = _moment_or_none(invalid_at, "invalid_at")
    expired_at = _moment_or_none(expired_at, "expired_at")
    if invalid_at and lesson_cache.parse_moment(invalid_at) <= lesson_cache.parse_moment(valid_at):
        raise ValueError(f"edge `invalid_at` ({invalid_at}) must be after `valid_at` ({valid_at})")
    weight = _clamp_weight(weight)

    with batch(root) as txn:
        onto = txn.onto
        rel = onto.relation(relation)
        if onto.is_inverse(relation):
            src, dst = dst, src
        relation = rel.name if rel.name in RELATIONS or rel.name in onto.relations else FALLBACK_RELATION
        src, dst = _clean_id(onto.canonical_id(src)), _clean_id(onto.canonical_id(dst))
        for node_id in (src, dst):
            if node_id not in txn.nodes:
                prefix = node_id.split(":", 1)[0] if ":" in node_id else ""
                _put_node(txn, node_id, prefix if prefix in onto.entity_types else "concept", "", None, provenance)
        problems = onto.check_edge(rel, txn.nodes[src].entity_type, txn.nodes[dst].entity_type)
        if problems:
            properties = {**(properties or {}), "ontology_warnings": problems}
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
                    _close(edge, valid_at, now_iso, f"restated from {valid_at}")
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
        if rel.exclusive:
            _resolve_exclusive(txn, new_edge, now_iso)
        txn.edges.append(new_edge)
        txn.by_key.setdefault(key, []).append(new_edge)
        txn.by_source_relation.setdefault((src, relation), []).append(new_edge)
        txn.by_entity.setdefault(src, []).append(new_edge)
        if dst != src:
            txn.by_entity.setdefault(dst, []).append(new_edge)
        txn.edges_dirty = True
        _provenance(txn, "edge", edge_id, provenance)
        return new_edge


def _close(edge: GraphEdge, at: str, recorded: str, reason: str) -> None:
    """End an edge's validity at `at`, noting when the store learned it and why."""
    edge.invalid_at = at
    edge.properties = {**edge.properties, "closed_recorded_at": recorded, "closed_reason": reason}


def _resolve_exclusive(txn: _Txn, new_edge: GraphEdge, now_iso: str) -> None:
    """An exclusive relation holds one target at a time: the latest valid_at wins. An
    older value ends where the newer begins; an assertion older than the current
    value is kept as history, ending where the current one begins."""
    start = lesson_cache.parse_moment(new_edge.valid_at)
    for edge in txn.by_source_relation.get((new_edge.source, new_edge.relation), []):
        if edge.target == new_edge.target or edge is new_edge:
            continue
        other_start = lesson_cache.parse_moment(edge.valid_at or edge.created_at)
        other_end = lesson_cache.parse_moment(edge.invalid_at) if edge.invalid_at else None
        if other_start <= start and (other_end is None or other_end > start):
            _close(edge, new_edge.valid_at, now_iso, f"superseded by {new_edge.target}")
            txn.edges_dirty = True
        elif other_start > start:
            end = new_edge.invalid_at
            if end is None or lesson_cache.parse_moment(end) > other_start:
                new_edge.invalid_at = edge.valid_at
                new_edge.properties = {**new_edge.properties, "closed_reason": f"superseded by {edge.target}",
                                       "closed_recorded_at": now_iso}


def _is_active_edge(
    edge: GraphEdge, moment: datetime | None, known_at: datetime | None = None,
    *, current_at: datetime | None = None,
) -> bool:
    """Valid at `moment` (valid time), as the store knew it at `known_at` (record time):
    an edge recorded later is unknown then, and a close recorded later had not happened."""
    def _at(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return lesson_cache.parse_moment(value)
        except ValueError:
            return None

    invalid_at = edge.invalid_at
    if known_at is not None:
        created = _at(edge.created_at)
        if created is not None and created > known_at:
            return False
        closed = _at(edge.properties.get("closed_recorded_at")) if edge.properties else None
        if closed is not None and closed > known_at:
            invalid_at = None
    if moment is None:
        moment = current_at or known_at or datetime.now(timezone.utc)
    start = _at(edge.valid_at)
    if start is not None and start > moment:
        return False
    for end in (_at(invalid_at), _at(edge.expired_at)):
        if end is not None and end <= moment:
            return False
    return True


_GRAPH_ADJ_CACHE: dict[tuple, tuple] = {}
_GRAPH_ADJ_CACHE_MAX_ENTRIES = 64
_GRAPH_INDEX_CACHE: dict[tuple, "_GraphFullIndex"] = {}
_GRAPH_NODE_CACHE: dict[tuple, "_GraphNodeIndex"] = {}
_GRAPH_CACHE_LOCK = threading.RLock()


def _clear_graph_cache() -> None:
    with _GRAPH_CACHE_LOCK:
        _GRAPH_ADJ_CACHE.clear()
        _GRAPH_INDEX_CACHE.clear()
        _GRAPH_NODE_CACHE.clear()
        _PHRASE_INDEX.clear()
        _SCANNED_ONCE.clear()


def _file_stamp(path: str) -> tuple[int, ...]:
    try:
        st = os.stat(path)
        return (int(st.st_dev), int(st.st_ino), int(st.st_mtime_ns), int(st.st_ctime_ns), int(st.st_size))
    except OSError:
        return (0, 0, 0, 0, 0)


def _graph_files_stamp(root: str) -> tuple[int, ...]:
    return _file_stamp(_nodes_file(root)) + _file_stamp(_edges_file(root))


def _version_chains(nodes: dict[str, GraphNode]) -> dict[str, list[GraphNode]]:
    chains: dict[str, list[GraphNode]] = {}
    for node in nodes.values():
        chains.setdefault(node.root_id or node.id, []).append(node)
    for chain in chains.values():
        chain.sort(key=lambda node: node.version)
    return chains


@dataclass
class _GraphNodeIndex:
    nodes: dict[str, GraphNode]
    stamp: tuple[int, ...]

    @cached_property
    def chains(self) -> dict[str, list[GraphNode]]:
        return _version_chains(self.nodes)


@dataclass
class _GraphFullIndex:
    """Incident edges eager; query-specific subsidiary indexes built on demand."""

    nodes: dict[str, GraphNode]
    edges: list[GraphEdge]
    by_entity: dict[str, list[int]]
    stamp: tuple[int, ...] = ()
    _moment_memo: dict[str, Any] = field(default_factory=dict, repr=False)

    @cached_property
    def begins(self) -> list[Any]:
        return [_parsed_moment(edge.valid_at or edge.created_at, self._moment_memo) for edge in self.edges]

    @cached_property
    def ends(self) -> list[Any]:
        result = []
        for edge in self.edges:
            moments = [_parsed_moment(value, self._moment_memo)
                       for value in (edge.invalid_at, edge.expired_at) if value]
            result.append(min((moment for moment in moments if moment is not None), default=None))
        return result

    @cached_property
    def by_relation(self) -> dict[str, list[int]]:
        result: dict[str, list[int]] = {}
        for position, edge in enumerate(self.edges):
            result.setdefault(edge.relation, []).append(position)
        return result

    @cached_property
    def by_source_relation(self) -> dict[tuple[str, str], list[int]]:
        result: dict[tuple[str, str], list[int]] = {}
        for position, edge in enumerate(self.edges):
            result.setdefault((edge.source, edge.relation), []).append(position)
        return result

    @cached_property
    def order_by_valid(self) -> list[int]:
        return sorted(range(len(self.edges)), key=lambda position: self.begins[position] or _MIN_MOMENT)

    @cached_property
    def sorted_begins(self) -> list[Any]:
        return [self.begins[position] or _MIN_MOMENT for position in self.order_by_valid]

    @cached_property
    def chains(self) -> dict[str, list[GraphNode]]:
        return _version_chains(self.nodes)


_MIN_MOMENT = datetime.min.replace(tzinfo=timezone.utc)


def _parsed_moment(value: Any, memo: dict[str, Any]) -> Any:
    key = str(value)
    if key not in memo:
        try:
            memo[key] = lesson_cache.parse_moment(value)
        except ValueError:
            memo[key] = None
    return memo[key]


def _build_full_index(nodes: dict[str, GraphNode], all_edges: list[GraphEdge]) -> _GraphFullIndex:
    """Build only incident lists; timestamp/relation/chain indexes remain lazy."""
    by_entity: dict[str, list[int]] = {}
    for idx, edge in enumerate(all_edges):
        by_entity.setdefault(edge.source, []).append(idx)
        if edge.target != edge.source:
            by_entity.setdefault(edge.target, []).append(idx)
    return _GraphFullIndex(nodes=nodes, edges=all_edges, by_entity=by_entity)


def _cached_graph_full(
    root: str, as_of: str | None = None, known_at: str | None = None,
) -> tuple[dict[str, GraphNode], list[GraphEdge], dict[str, list[GraphEdge]], list[GraphEdge]]:
    """Cached (nodes, active, adj, all_edges); single load_nodes/load_edges per stamp."""
    index = _full_index(root)
    key = (os.path.abspath(str(root)), index.stamp, as_of or "", known_at or "")
    current = not as_of and not known_at
    now = datetime.now(timezone.utc) if current else None
    with _GRAPH_CACHE_LOCK:
        hit = _GRAPH_ADJ_CACHE.get(key)
        if hit is not None:
            # A current-time view changes when a TTL passes, even without a
            # source write. Historical views have no wall-clock deadline.
            if current and len(hit) > 4 and (now < hit[4] or (hit[5] is not None and now >= hit[5])):
                _GRAPH_ADJ_CACHE.pop(key, None)
            elif len(hit) >= 4:
                return hit[0], hit[1], hit[2], hit[3]
            else:
                return hit[0], hit[1], hit[2], []
    nodes, all_edges = index.nodes, index.edges
    moment = lesson_cache.parse_moment(as_of) if as_of else None
    known = lesson_cache.parse_moment(known_at) if known_at else None
    active = [e for e in all_edges if _is_active_edge(e, moment, known, current_at=now)]
    adj: dict[str, list[GraphEdge]] = {}
    for e in active:
        adj.setdefault(e.source, []).append(e)
        if e.target != e.source:
            adj.setdefault(e.target, []).append(e)
    deadlines = []
    if current:
        # Scheduled facts can start or close without a file write, including
        # currently inactive edges. Evaluate every boundary against the same
        # instant used to construct this view.
        for edge in all_edges:
            for boundary in (edge.valid_at, edge.invalid_at, edge.expired_at):
                if not boundary:
                    continue
                try:
                    deadline = lesson_cache.parse_moment(boundary)
                except ValueError:
                    continue
                if deadline > now:
                    deadlines.append(deadline)
    value = (nodes, active, adj, all_edges, now, min(deadlines) if deadlines else None)
    with _GRAPH_CACHE_LOCK:
        if _graph_files_stamp(root) == index.stamp:
            if len(_GRAPH_ADJ_CACHE) >= _GRAPH_ADJ_CACHE_MAX_ENTRIES:
                _GRAPH_ADJ_CACHE.pop(next(iter(_GRAPH_ADJ_CACHE)))
            _GRAPH_ADJ_CACHE[key] = value
    return nodes, active, adj, all_edges


def _cached_graph(root: str, as_of: str | None = None, known_at: str | None = None) -> tuple:
    nodes, active, adj, _all_edges = _cached_graph_full(root, as_of, known_at)
    return nodes, active, adj


@contextlib.contextmanager
def _snapshot_guard(root: str) -> Iterator[None]:
    """Reuse the graph mutation lock, with identity checks for read-only stores."""
    with contextlib.ExitStack() as guards:
        if os.path.isdir(_graph_dir(root)):
            try:
                guards.enter_context(_jsonl.locked(_lock_file(root)))
            except OSError as exc:
                if not isinstance(exc, PermissionError) and exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
                    raise
        yield


def _node_index(root: str) -> _GraphNodeIndex:
    """A coherent node-only snapshot; unrelated edges need not be read."""
    base = os.path.abspath(str(root))
    stamp = _file_stamp(_nodes_file(root))
    with _GRAPH_CACHE_LOCK:
        hit = _GRAPH_NODE_CACHE.get((base, stamp))
        if hit is not None:
            return hit
    with _snapshot_guard(root):
        stamp = _file_stamp(_nodes_file(root))
        with _GRAPH_CACHE_LOCK:
            hit = _GRAPH_NODE_CACHE.get((base, stamp))
            if hit is not None:
                return hit
        for _attempt in range(3):
            stamp = _file_stamp(_nodes_file(root))
            nodes = load_nodes(root)
            if _file_stamp(_nodes_file(root)) == stamp:
                break
        else:
            raise RuntimeError("graph nodes changed repeatedly during snapshot; retry the query")
    index = _GraphNodeIndex(nodes, stamp)
    with _GRAPH_CACHE_LOCK:
        if _file_stamp(_nodes_file(root)) == stamp:
            if len(_GRAPH_NODE_CACHE) >= _GRAPH_ADJ_CACHE_MAX_ENTRIES:
                _GRAPH_NODE_CACHE.pop(next(iter(_GRAPH_NODE_CACHE)))
            _GRAPH_NODE_CACHE[(base, stamp)] = index
    return index


def _full_index(root: str) -> _GraphFullIndex:
    """One immutable source generation shared by current and historical reads."""
    base = os.path.abspath(str(root))
    stamp = _graph_files_stamp(root)
    with _GRAPH_CACHE_LOCK:
        hit = _GRAPH_INDEX_CACHE.get((base, stamp))
        if hit is not None:
            return hit
    # Share the mutation lock while capturing both source files. Expensive
    # indexing happens after release; a later writer cannot relabel this older
    # generation as its own. Missing stores need no lock file or directories.
    with _snapshot_guard(root):
        stamp = _graph_files_stamp(root)
        with _GRAPH_CACHE_LOCK:
            hit = _GRAPH_INDEX_CACHE.get((base, stamp))
            if hit is not None:
                return hit
        for _attempt in range(3):
            stamp = _graph_files_stamp(root)
            nodes, all_edges = _node_index(root).nodes, load_edges(root)
            if _graph_files_stamp(root) == stamp:
                break
        else:
            raise RuntimeError("graph source changed repeatedly during snapshot; retry the query")
    index = _build_full_index(nodes, all_edges)
    index.stamp = stamp
    key = (base, stamp)
    with _GRAPH_CACHE_LOCK:
        if _graph_files_stamp(root) == stamp:
            if len(_GRAPH_INDEX_CACHE) >= _GRAPH_ADJ_CACHE_MAX_ENTRIES:
                _GRAPH_INDEX_CACHE.pop(next(iter(_GRAPH_INDEX_CACHE)))
            _GRAPH_INDEX_CACHE[key] = index
    return index


def get_neighbors(
    root: str,
    node_id: str,
    direction: str = "both",
    relation: str | None = None,
    as_of: str | None = None,
    known_at: str | None = None,
) -> list[dict[str, Any]]:
    """Nodes one edge away from *node_id*, with the connecting relation."""
    clean_id = _clean_id(node_id)
    index = _full_index(root)
    nodes = index.nodes
    moment = lesson_cache.parse_moment(as_of) if as_of else None
    known = lesson_cache.parse_moment(known_at) if known_at else None
    current_at = datetime.now(timezone.utc) if moment is None and known is None else None
    results: list[dict[str, Any]] = []
    for position in index.by_entity.get(clean_id, []):
        edge = index.edges[position]
        if not _is_active_edge(edge, moment, known, current_at=current_at):
            continue
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
    known_at: str | None = None,
) -> dict[str, Any]:
    """Breadth-first subgraph within *max_hops* of the start nodes."""
    index = _full_index(root)
    nodes = index.nodes
    moment = lesson_cache.parse_moment(as_of) if as_of else None
    known = lesson_cache.parse_moment(known_at) if known_at else None
    current_at = datetime.now(timezone.utc) if moment is None and known is None else None
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
        for position in index.by_entity.get(current, []):
            edge = index.edges[position]
            if not _is_active_edge(edge, moment, known, current_at=current_at):
                continue
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
    terms += [str(a).lower() for a in (node.properties or {}).get("aliases", [])[:20] if isinstance(a, str)]
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
    snapshot = _node_index(root)
    key = (os.path.abspath(str(root)), snapshot.stamp)
    with _GRAPH_CACHE_LOCK:
        hit = _PHRASE_INDEX.get(key)
        if hit is not None:
            return hit
    index: dict[str, list[tuple[tuple[str, ...], str]]] = {}
    for nid, node in snapshot.nodes.items():
        if node.is_forgotten:
            continue
        for words in set(_node_terms(nid, node)):
            index.setdefault(words[0], []).append((words, nid))
    with _GRAPH_CACHE_LOCK:
        if _file_stamp(_nodes_file(root)) == snapshot.stamp:
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
    snapshot = _node_index(root)
    key = (os.path.abspath(str(root)), snapshot.stamp)
    with _GRAPH_CACHE_LOCK:
        first_scan = key not in _PHRASE_INDEX and key not in _SCANNED_ONCE
        if first_scan:
            _SCANNED_ONCE.add(key)
    if first_scan:
        norm = " " + " ".join(words) + " "
        return sorted(
            nid for nid, node in snapshot.nodes.items()
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


def edges_between(
    root: str, start: str | None, end: str | None, *, relation: str | None = None, entity: str | None = None,
) -> list[GraphEdge]:
    """Edges valid at some moment in [start, end] (either may be open), oldest first."""
    lo = lesson_cache.parse_moment(start) if start else None
    hi = lesson_cache.parse_moment(end) if end else None
    if lo and hi and hi < lo:
        raise ValueError("the interval ends before it starts")
    ent = _clean_id(entity) if entity else None
    index = _full_index(root)
    edges = index.edges
    begins = index.begins if ent is None else index.__dict__.get("begins")
    ends = index.ends if ent is None else index.__dict__.get("ends")
    if ent is not None and relation is not None:
        by_ent = index.by_entity.get(ent, [])
        by_rel = index.by_relation.get(relation, [])
        cand: Any = by_ent if len(by_ent) <= len(by_rel) else by_rel
    elif ent is not None:
        cand: Any = index.by_entity.get(ent, [])
    elif relation is not None:
        cand = index.by_relation.get(relation, [])
    elif hi is not None:
        pos = bisect.bisect_right(index.sorted_begins, hi)
        cand = index.order_by_valid[:pos]
    else:
        cand = range(len(edges))
    out: list[GraphEdge] = []
    for i in cand:
        edge = edges[i]
        if relation and edge.relation != relation:
            continue
        if ent and ent not in (edge.source, edge.target):
            continue
        begin = (begins[i] if begins is not None
                 else _parsed_moment(edge.valid_at or edge.created_at, index._moment_memo))
        if begin is None:
            begin = lesson_cache.parse_moment(edge.valid_at or edge.created_at)
        if hi is not None and begin > hi:
            continue
        if ends is not None:
            finish = ends[i]
        else:
            candidates = [_parsed_moment(value, index._moment_memo)
                          for value in (edge.invalid_at, edge.expired_at) if value]
            finish = min((moment for moment in candidates if moment is not None), default=None)
        if lo is not None and finish is not None and finish <= lo:
            continue
        out.append(edge)
    return copy.deepcopy(sorted(out, key=lambda e: e.valid_at or e.created_at))


def timeline(root: str, entity: str) -> list[dict[str, Any]]:
    """Every change to an entity's relations in valid-time order: what began, what
    ended, and why, so a fact's evolution reads top to bottom."""
    ent = _clean_id(entity)
    index = _full_index(root)
    edges = index.edges
    decorated: list[tuple[Any, bool, dict[str, Any]]] = []
    for i in index.by_entity.get(ent, []):
        edge = edges[i]
        base = {"source": edge.source, "relation": edge.relation, "target": edge.target}
        begin = _parsed_moment(edge.valid_at or edge.created_at, index._moment_memo)
        if begin is None:
            begin = lesson_cache.parse_moment(edge.valid_at or edge.created_at)
        decorated.append((begin, True, {**base, "at": edge.valid_at or edge.created_at, "event": "began",
                                        "recorded_at": edge.created_at}))
        if edge.invalid_at:
            finish = lesson_cache.parse_moment(edge.invalid_at)
            decorated.append((finish, False, {**base, "at": edge.invalid_at, "event": "ended",
                                              "reason": (edge.properties or {}).get("closed_reason", ""),
                                              "recorded_at": (edge.properties or {}).get("closed_recorded_at", "")}))
        if edge.expired_at and edge.expired_at != edge.invalid_at:
            finish = lesson_cache.parse_moment(edge.expired_at)
            decorated.append((finish, False, {**base, "at": edge.expired_at, "event": "expired",
                                              "reason": (edge.properties or {}).get("expired_reason", ""),
                                              "recorded_at": edge.created_at}))
    decorated.sort(key=lambda t: (t[0], t[1]))
    return [event for _moment, _is_began, event in decorated]


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
    index = _node_index(root)
    target = index.nodes.get(_clean_id(node_id))
    if target is None:
        return []
    root_id = target.root_id or target.id
    return copy.deepcopy(index.chains.get(root_id, []))


def list_version_chains(root: str) -> dict[str, list[GraphNode]]:
    """Nodes grouped by version-chain root, each chain oldest first."""
    index = _node_index(root)
    return copy.deepcopy(index.chains)


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
        for edge in txn.by_entity.get(clean_id, []):
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
