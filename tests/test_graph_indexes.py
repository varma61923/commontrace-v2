"""Index-backed graph reads: equivalence, scaling, and invalidation."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from commontrace import graph, lesson_cache

BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _seed(root: str, n_edges: int = 400, n_entities: int = 40) -> None:
    with graph.batch(root):
        for i in range(n_entities):
            graph.add_node(root, f"svc:{i}", "service", f"Service {i}")
            graph.add_node(root, f"tool:{i}", "tool", f"Tool {i}")
        for i in range(n_edges):
            src = f"svc:{i % n_entities}"
            dst = f"tool:{(i * 7) % n_entities}"
            rel = graph.RELATIONS[i % len(graph.RELATIONS)]
            valid_at = (BASE + timedelta(hours=i)).isoformat()
            kwargs = {"valid_at": valid_at, "weight": 0.1 + (i % 10) / 10.0}
            if i % 11 == 0:
                kwargs["invalid_at"] = (BASE + timedelta(hours=i, days=30)).isoformat()
            graph.add_edge(root, src, dst, rel, **kwargs)
        graph.add_node(root, "memory:chain", "memory", "Chain")
        chain_id = "memory:chain"
        for v in range(3):
            node = graph.create_memory_version(root, chain_id, new_name=f"Chain v{v + 2}")
            chain_id = node.id


def _old_edges_between(root, start, end, relation=None, entity=None):
    lo = lesson_cache.parse_moment(start) if start else None
    hi = lesson_cache.parse_moment(end) if end else None
    if lo and hi and hi < lo:
        raise ValueError("the interval ends before it starts")
    ent = graph._clean_id(entity) if entity else None
    out = []
    for edge in graph.load_edges(root):
        if relation and edge.relation != relation:
            continue
        if ent and ent not in (edge.source, edge.target):
            continue
        begins = lesson_cache.parse_moment(edge.valid_at or edge.created_at)
        ends = lesson_cache.parse_moment(edge.invalid_at) if edge.invalid_at else None
        if hi is not None and begins > hi:
            continue
        if lo is not None and ends is not None and ends <= lo:
            continue
        out.append(edge)
    return sorted(out, key=lambda e: e.valid_at or e.created_at)


def _old_timeline(root, entity):
    ent = graph._clean_id(entity)
    events: list[dict] = []
    for edge in graph.load_edges(root):
        if ent not in (edge.source, edge.target):
            continue
        base = {"source": edge.source, "relation": edge.relation, "target": edge.target}
        events.append({**base, "at": edge.valid_at or edge.created_at, "event": "began",
                       "recorded_at": edge.created_at})
        if edge.invalid_at:
            events.append({**base, "at": edge.invalid_at, "event": "ended",
                           "reason": (edge.properties or {}).get("closed_reason", ""),
                           "recorded_at": (edge.properties or {}).get("closed_recorded_at", "")})
    return sorted(events, key=lambda e: (lesson_cache.parse_moment(e["at"]), e["event"] == "began"))


def _old_get_version_chain(root, node_id):
    nodes = graph.load_nodes(root)
    target = nodes.get(graph._clean_id(node_id))
    if target is None:
        return []
    root_id = target.root_id or target.id
    chain = [n for n in nodes.values() if n.root_id == root_id or n.id == root_id]
    return sorted(chain, key=lambda n: n.version)


def _old_list_version_chains(root):
    chains: dict = {}
    for node in graph.load_nodes(root).values():
        chains.setdefault(node.root_id or node.id, []).append(node)
    for chain in chains.values():
        chain.sort(key=lambda n: n.version)
    return chains


def test_edges_between_equivalence(tmp_path):
    root = str(tmp_path / "g1")
    _seed(root, n_edges=400)
    queries = [
        (None, None, None, None),
        ("2024-01-05T00:00:00+00:00", "2024-01-10T00:00:00+00:00", None, None),
        (None, "2024-02-01T00:00:00+00:00", "uses", None),
        ("2024-01-01T00:00:00+00:00", None, None, "svc:3"),
        ("2024-01-02T00:00:00+00:00", "2024-03-01T00:00:00+00:00", "causes", "tool:5"),
        (None, None, "depends_on", "svc:7"),
    ]
    for start, end, rel, ent in queries:
        new = graph.edges_between(root, start, end, relation=rel, entity=ent)
        old = _old_edges_between(root, start, end, relation=rel, entity=ent)
        assert [e.to_dict() for e in new] == [e.to_dict() for e in old]


def test_timeline_equivalence(tmp_path):
    root = str(tmp_path / "g2")
    _seed(root, n_edges=400)
    for ent in ("svc:0", "svc:3", "tool:5", "svc:39", "missing:thing"):
        assert graph.timeline(root, ent) == _old_timeline(root, ent)


def test_version_chain_equivalence(tmp_path):
    root = str(tmp_path / "g3")
    _seed(root, n_edges=200)
    latest = graph.get_version_chain(root, "memory:chain")[-1].id
    for nid in ("memory:chain", latest, "svc:1", "missing:node"):
        new = [n.to_dict() for n in graph.get_version_chain(root, nid)]
        old = [n.to_dict() for n in _old_get_version_chain(root, nid)]
        assert new == old
    new_all = {k: [n.to_dict() for n in v] for k, v in graph.list_version_chains(root).items()}
    old_all = {k: [n.to_dict() for n in v] for k, v in _old_list_version_chains(root).items()}
    assert new_all == old_all


def test_forget_node_uses_index(tmp_path):
    root = str(tmp_path / "g4")
    _seed(root, n_edges=300)
    before = graph.load_edges(root)
    touching = [e for e in before if "svc:2" in (e.source, e.target)]
    assert touching
    node = graph.forget_node(root, "svc:2", reason="stale")
    assert node is not None and node.is_forgotten is True
    after = graph.load_edges(root)
    closed = [e for e in after if e.source == "svc:2" or e.target == "svc:2"]
    assert closed and all(e.invalid_at is not None for e in closed)
    untouched_before = {(e.source, e.target, e.relation, e.valid_at) for e in before if "svc:2" not in (
        e.source, e.target)}
    untouched_after = {(e.source, e.target, e.relation, e.valid_at) for e in after if "svc:2" not in (
        e.source, e.target)}
    assert untouched_before == untouched_after
    restored = graph.forget_node(root, "svc:2", undo=True)
    assert restored is not None and restored.is_forgotten is False


def test_scaling_timeline_neighbors_fast(tmp_path):
    root = str(tmp_path / "g5")
    _seed(root, n_edges=2000, n_entities=100)
    graph.timeline(root, "svc:0")
    graph.get_neighbors(root, "svc:0")
    start = time.perf_counter()
    graph.timeline(root, "svc:1")
    graph.timeline(root, "tool:3")
    graph.edges_between(root, "2024-01-10T00:00:00+00:00", "2024-02-01T00:00:00+00:00", entity="svc:1")
    graph.get_neighbors(root, "svc:1")
    graph.get_version_chain(root, "memory:chain")
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"indexed reads took {elapsed:.3f}s"


def test_expired_edges_are_excluded_from_temporal_interval_reads(tmp_path):
    root = str(tmp_path / "g-expiry")
    graph.add_edge(root, "svc:a", "tool:a", "uses", valid_at="2024-01-01",
                   expired_at="2024-06-01")
    assert graph.edges_between(root, "2024-02-01", "2024-05-01")
    assert graph.edges_between(root, "2024-07-01", None) == []


def test_invalidation_write_read(tmp_path):
    root = str(tmp_path / "g6")
    _seed(root, n_edges=100)
    first = graph.timeline(root, "svc:9")
    graph.add_edge(root, "svc:9", "tool:9", "uses", valid_at=(BASE + timedelta(days=400)).isoformat())
    second = graph.timeline(root, "svc:9")
    assert len(second) > len(first)
    assert any(e["at"].startswith("2025") for e in second)
    between = graph.edges_between(root, "2025-01-01T00:00:00+00:00", None, entity="svc:9")
    assert any(e.source == "svc:9" for e in between)


def test_invalidation_batch_commit(tmp_path):
    root = str(tmp_path / "g7")
    _seed(root, n_edges=100)
    before = len(graph.edges_between(root, None, None, entity="svc:4"))
    with graph.batch(root):
        for i in range(20):
            graph.add_edge(root, "svc:4", f"tool:batch{i % 5}", "relates_to",
                           valid_at=(BASE + timedelta(days=500, hours=i)).isoformat())
    after = len(graph.edges_between(root, None, None, entity="svc:4"))
    assert after > before
    chain_before = len(graph.get_version_chain(root, "memory:chain"))
    latest = graph.get_version_chain(root, "memory:chain")[-1].id
    graph.create_memory_version(root, latest, new_name="Chain v5")
    assert len(graph.get_version_chain(root, "memory:chain")) == chain_before + 1


def test_index_parses_once(tmp_path):
    root = str(tmp_path / "g8")
    _seed(root, n_edges=200)
    index = graph._full_index(root)
    assert len(index.begins) == len(index.edges) == len(graph.load_edges(root))
    assert all(b is not None for b in index.begins)
    assert "svc:1" in index.by_entity
    assert index.chains
    assert len(index.order_by_valid) == len(index.edges)
