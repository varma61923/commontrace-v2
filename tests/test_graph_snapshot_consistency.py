"""Graph snapshots remain exact, generation-safe, and isolated from callers."""
from __future__ import annotations

import contextlib
import errno
import os
from datetime import datetime, timedelta, timezone

import pytest

from commontrace import graph, lesson_cache

BASE = "2024-01-01T00:00:00+00:00"


def _seed(root, count=120):
    nodes = {node: graph.GraphNode(node, "concept", node, {}, BASE, BASE)
             for node in ("target", "peer", "unrelated")}
    edges = [graph.GraphEdge("target", "peer", "uses", 1, BASE, None, None,
                             {"nested": {"key": "original"}}, BASE)]
    edges += [graph.GraphEdge("unrelated", f"tool:{i}", "uses", 1, BASE, None, None,
                              created_at=BASE) for i in range(count)]
    graph.save_nodes(root, nodes)
    graph.save_edges(root, edges)
    graph._clear_graph_cache()
    return nodes, edges


def test_distinct_temporal_requests_share_source_and_check_only_incident_edges(tmp_path, monkeypatch):
    root = str(tmp_path)
    _nodes, edges = _seed(root)
    reads = {"nodes": 0, "edges": 0, "eligible": 0}
    for name in ("nodes", "edges"):
        original = getattr(graph, f"load_{name}")

        def counted(*args, _name=name, _original=original, **kwargs):
            reads[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(graph, f"load_{name}", counted)
    original_active = graph._is_active_edge

    def active(*args, **kwargs):
        reads["eligible"] += 1
        return original_active(*args, **kwargs)

    monkeypatch.setattr(graph, "_is_active_edge", active)
    for date in ("2023-01-01", "2024-01-01", "2024-03-01", "2025-01-01"):
        expected = original_active(edges[0], lesson_cache.parse_moment(date), None)
        neighbors = graph.get_neighbors(root, "target", as_of=date, known_at=date)
        subgraph = graph.multi_hop_subgraph(root, ["target"], as_of=date, known_at=date, max_hops=1)
        assert len(neighbors) == int(expected)
        assert subgraph["edges"] == ([edges[0].to_dict()] if expected else [])
        assert subgraph["hop_distances"] == ({"target": 0, "peer": 1} if expected else {"target": 0})
    assert reads == {"nodes": 1, "edges": 1, "eligible": 8}


def test_source_change_during_read_cannot_be_published_as_new_generation(tmp_path, monkeypatch):
    root = str(tmp_path)
    _nodes, edges = _seed(root)
    original = graph.load_edges
    changed = False

    def racing_load(store):
        nonlocal changed
        out = original(store)
        if not changed:
            changed = True
            edges[0].relation = "depends_on"
            graph.save_edges(store, edges)
        return out

    monkeypatch.setattr(graph, "load_edges", racing_load)
    assert graph.get_neighbors(root, "target")[0]["relation"] == "depends_on"
    assert graph.get_neighbors(root, "target")[0]["relation"] == "depends_on"


def test_same_size_atomic_replacement_with_restored_mtime_invalidates_cache(tmp_path):
    root = str(tmp_path)
    _nodes, edges = _seed(root)
    assert graph.get_neighbors(root, "target")[0]["relation"] == "uses"
    path = graph._edges_file(root)
    original = os.stat(path)
    edges[0].relation = "owns"  # Same byte length as uses; imported relations are preserved.
    graph.save_edges(root, edges)
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert os.stat(path).st_size == original.st_size
    assert graph.get_neighbors(root, "target")[0]["relation"] == "owns"


@pytest.mark.parametrize("method", ["edges", "chain", "chains"])
def test_public_objects_cannot_mutate_cached_graph(tmp_path, method):
    root = str(tmp_path)
    _seed(root)
    if method == "edges":
        result = graph.edges_between(root, None, None, entity="target")
        result[0].relation = "corrupted"
        result[0].properties["nested"]["key"] = "changed"
        fresh = graph.edges_between(root, None, None, entity="target")[0]
        assert fresh.relation == "uses"
        assert fresh.properties["nested"]["key"] == "original"
    else:
        result = (graph.get_version_chain(root, "target") if method == "chain"
                  else graph.list_version_chains(root)["target"])
        result[0].name = "corrupted"
        result[0].properties["secret"] = "changed"
        fresh = graph.get_version_chain(root, "target")[0]
        assert fresh.name == "target"
        assert fresh.properties == {}


@pytest.mark.parametrize("error", [PermissionError("read-only snapshot"), OSError(errno.EROFS, "read-only mount")])
def test_read_only_store_does_not_require_creation_of_lock(tmp_path, monkeypatch, error):
    root = str(tmp_path)
    _seed(root)

    @contextlib.contextmanager
    def cannot_lock(_path):
        raise error
        yield  # pragma: no cover

    monkeypatch.setattr(graph._jsonl, "locked", cannot_lock)
    assert graph.get_neighbors(root, "target")[0]["neighbor_id"] == "peer"


def test_graph_reads_propagate_unexpected_lock_io_errors(tmp_path, monkeypatch):
    root = str(tmp_path)
    _seed(root)

    @contextlib.contextmanager
    def cannot_lock(_path):
        raise OSError(errno.EIO, "broken storage")
        yield  # pragma: no cover

    monkeypatch.setattr(graph._jsonl, "locked", cannot_lock)
    with pytest.raises(OSError, match="broken storage"):
        graph.get_neighbors(root, "target")


def test_roots_with_identical_ids_do_not_share_snapshots(tmp_path):
    first, second = str(tmp_path / "first"), str(tmp_path / "second")
    _seed(first)
    _nodes, edges = _seed(second)
    edges[0].relation = "affects"
    graph.save_edges(second, edges)
    assert graph.get_neighbors(first, "target")[0]["relation"] == "uses"
    assert graph.get_neighbors(second, "target")[0]["relation"] == "affects"


def test_cached_current_exports_expire_without_source_write(tmp_path, monkeypatch):
    root = str(tmp_path)
    _nodes, edges = _seed(root, count=0)
    clock = [datetime(2024, 1, 2, tzinfo=timezone.utc)]
    edges[0].expired_at = (clock[0] + timedelta(seconds=1)).isoformat()
    graph.save_edges(root, edges)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(graph, "datetime", Clock)
    assert graph.export_json(root)["total_active_edges"] == 1
    clock[0] += timedelta(seconds=1)
    assert graph.export_json(root)["total_active_edges"] == 0
    assert graph.export_json(root, as_of=BASE)["total_active_edges"] == 1
    clock[0] -= timedelta(seconds=2)  # Clock rollback must rebuild the current view too.
    assert graph.export_json(root)["total_active_edges"] == 1


def test_scheduled_update_starts_and_closes_at_effective_time_without_source_write(tmp_path, monkeypatch):
    root = str(tmp_path)
    _nodes, _edges = _seed(root, count=0)
    transition = "2030-01-01T00:00:00+00:00"
    expiry = "2031-01-01T00:00:00+00:00"
    edges = [graph.GraphEdge("target", "London", "lives_in", 1, BASE, transition, None, created_at=BASE),
             graph.GraphEdge("target", "Paris", "lives_in", 1, transition, None, expiry, created_at=BASE)]
    graph.save_edges(root, edges)
    clock = [datetime(2029, 1, 1, tzinfo=timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(graph, "datetime", Clock)
    assert [edge["target"] for edge in graph.export_json(root)["edges"]] == ["London"]
    assert [edge["neighbor_id"] for edge in graph.get_neighbors(root, "target")] == ["London"]
    assert [edge["target"] for edge in graph.multi_hop_subgraph(root, ["target"], max_hops=1)["edges"]] == ["London"]
    assert [edge["target"] for edge in graph.export_json(root, as_of=transition)["edges"]] == ["Paris"]
    clock[0] = datetime.fromisoformat(transition)
    assert [edge["target"] for edge in graph.export_json(root)["edges"]] == ["Paris"]
    clock[0] = datetime.fromisoformat(expiry)
    assert graph.export_json(root)["edges"] == []


@pytest.mark.parametrize("api", ["neighbors", "multi_hop", "export"])
def test_current_request_uses_one_clock_snapshot_across_scheduled_transition(tmp_path, monkeypatch, api):
    root = str(tmp_path)
    _seed(root, count=0)
    transition = "2030-01-01T00:00:00+00:00"
    graph.save_edges(root, [
        graph.GraphEdge("target", "London", "lives_in", 1, BASE, transition, None, created_at=BASE),
        graph.GraphEdge("target", "Paris", "lives_in", 1, transition, None, None, created_at=BASE),
    ])
    calls = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2029 if not calls else 2030, 1, 1, tzinfo=timezone.utc)
            calls.append(instant)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(graph, "datetime", Clock)
    if api == "neighbors":
        assert [edge["neighbor_id"] for edge in graph.get_neighbors(root, "target")] == ["London"]
    elif api == "multi_hop":
        assert [edge["target"] for edge in graph.multi_hop_subgraph(root, ["target"], max_hops=1)["edges"]] == ["London"]
    else:
        assert [edge["target"] for edge in graph.export_json(root)["edges"]] == ["London"]
    assert len(calls) == 1
