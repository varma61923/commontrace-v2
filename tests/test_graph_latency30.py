"""Demand-driven graph indexing keeps exact results and source-generation safety."""
from __future__ import annotations

import contextlib
import errno
import os

from commontrace import graph

BASE = "2024-01-01T00:00:00+00:00"


def _seed(root, count=200):
    nodes = {identity: graph.GraphNode(identity, "concept", name, {}, BASE, BASE)
             for identity, name in (("target", "Target Service"), ("peer", "Peer Tool"),
                                    ("unrelated", "Unrelated Service"))}
    edges = [graph.GraphEdge("target" if index < 3 else "unrelated", "peer", "uses", 1,
                            BASE, None, None, created_at=BASE) for index in range(count)]
    graph.save_nodes(root, nodes)
    graph.save_edges(root, edges)
    graph._clear_graph_cache()
    return nodes, edges


def test_node_only_apis_never_read_or_filter_edges(tmp_path, monkeypatch):
    root = str(tmp_path)
    _seed(root)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("node-only lookup touched edges")

    monkeypatch.setattr(graph, "load_edges", forbidden)
    monkeypatch.setattr(graph, "_is_active_edge", forbidden)
    assert graph.extract_entities_from_text(root, "Target Service uses Peer Tool") == ["peer", "target"]
    assert graph.extract_entities_from_text(root, "Target Service uses Peer Tool") == ["peer", "target"]
    assert [node.id for node in graph.get_version_chain(root, "target")] == ["target"]
    assert set(graph.list_version_chains(root)) == {"peer", "target", "unrelated"}
    assert not graph._GRAPH_INDEX_CACHE


def test_entity_queries_do_not_build_global_subsidiary_indexes(tmp_path):
    root = str(tmp_path)
    _seed(root)
    graph.get_neighbors(root, "target")
    index = graph._full_index(root)
    subsidiary = {"begins", "ends", "chains", "by_relation", "by_source_relation",
                  "order_by_valid", "sorted_begins"}
    assert not subsidiary.intersection(index.__dict__)
    assert len(graph.edges_between(root, BASE, "2025-01-01", entity="target")) == 3
    assert len(graph.timeline(root, "target")) == 3
    assert not subsidiary.intersection(index.__dict__)
    assert len(graph.edges_between(root, BASE, "2025-01-01", relation="uses", entity="target")) == 3
    assert subsidiary.intersection(index.__dict__) == {"by_relation"}
    assert len(graph.edges_between(root, BASE, "2025-01-01")) == 200
    assert {"begins", "ends", "order_by_valid", "sorted_begins"}.issubset(index.__dict__)


def test_node_generation_replacement_cannot_publish_stale_names(tmp_path, monkeypatch):
    root = str(tmp_path)
    nodes, _edges = _seed(root)
    nodes["target"].name = "Original Service"
    graph.save_nodes(root, nodes)
    original = graph.load_nodes
    changed = False

    def racing_load(store):
        nonlocal changed
        result = original(store)
        if not changed:
            changed = True
            nodes["target"].name = "Changed Service"
            graph.save_nodes(store, nodes)
        return result

    monkeypatch.setattr(graph, "load_nodes", racing_load)
    assert graph.extract_entities_from_text(root, "Original Service") == []
    assert graph.extract_entities_from_text(root, "Changed Service") == ["target"]


def test_node_stamp_detects_same_length_replacement_with_restored_mtime(tmp_path):
    root = str(tmp_path)
    nodes, _edges = _seed(root)
    nodes["target"].name = "Original"
    graph.save_nodes(root, nodes)
    assert graph.extract_entities_from_text(root, "Original") == ["target"]
    path = graph._nodes_file(root)
    stamp = os.stat(path)
    nodes["target"].name = "Modified"
    graph.save_nodes(root, nodes)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert os.stat(path).st_size == stamp.st_size
    assert graph.extract_entities_from_text(root, "Original") == []
    assert graph.extract_entities_from_text(root, "Modified") == ["target"]


def test_phrase_reader_does_not_republish_changed_generation(tmp_path, monkeypatch):
    root = str(tmp_path)
    nodes, _edges = _seed(root)
    nodes["target"].name = "Original Service"
    graph.save_nodes(root, nodes)
    original = graph._node_terms
    changed = False

    def racing_terms(identity, node):
        nonlocal changed
        if not changed:
            changed = True
            nodes["target"].name = "Changed Service"
            graph.save_nodes(root, nodes)
        return original(identity, node)

    monkeypatch.setattr(graph, "_node_terms", racing_terms)
    graph._phrase_index(root)
    assert not graph._PHRASE_INDEX
    assert graph.extract_entities_from_text(root, "Changed Service") == ["target"]
    assert graph.extract_entities_from_text(root, "Original Service") == []


def test_node_cache_shares_full_snapshot_and_ignores_unrelated_edge_changes(tmp_path, monkeypatch):
    root = str(tmp_path)
    _nodes, edges = _seed(root)
    original = graph.load_nodes
    reads = []

    def counted(store):
        reads.append(store)
        return original(store)

    monkeypatch.setattr(graph, "load_nodes", counted)
    assert graph.extract_entities_from_text(root, "Target Service") == ["target"]
    graph.get_neighbors(root, "target")
    edges[-1].weight = 0.5
    graph.save_edges(root, edges)
    assert graph.extract_entities_from_text(root, "Target Service") == ["target"]
    graph.get_neighbors(root, "target")
    assert len(reads) == 1


def test_node_only_reads_work_on_read_only_mount(tmp_path, monkeypatch):
    root = str(tmp_path)
    _seed(root)

    @contextlib.contextmanager
    def unavailable(_path):
        raise OSError(errno.EROFS, "read-only mount")
        yield  # pragma: no cover

    monkeypatch.setattr(graph._jsonl, "locked", unavailable)
    assert graph.extract_entities_from_text(root, "Target Service") == ["target"]
    assert graph.get_version_chain(root, "target")[0].name == "Target Service"


def test_global_timestamp_index_parses_distinct_moments_once(tmp_path, monkeypatch):
    root = str(tmp_path)
    _seed(root, count=1000)
    index = graph._full_index(root)
    original = graph.lesson_cache.parse_moment
    parsed = []

    def counted(value):
        parsed.append(value)
        return original(value)

    monkeypatch.setattr(graph.lesson_cache, "parse_moment", counted)
    assert len(index.begins) == 1000
    assert len(index.ends) == 1000
    assert len(index.sorted_begins) == 1000
    assert parsed == [BASE]


def test_combined_filters_use_rare_relation_pool_for_high_degree_entity(tmp_path):
    root = str(tmp_path)
    _nodes, edges = _seed(root, count=1000)
    for edge in edges:
        edge.source = "target"
    edges[-1].relation = "resolves"
    edges[-1].valid_at = "2024-06-01T00:00:00Z"
    graph.save_edges(root, edges)
    index = graph._full_index(root)
    visited = {"entity": 0, "relation": 0}

    class CountedPositions(list):
        def __init__(self, values, name):
            super().__init__(values)
            self.name = name

        def __iter__(self):
            for position in super().__iter__():
                visited[self.name] += 1
                yield position

    index.by_entity["target"] = CountedPositions(index.by_entity["target"], "entity")
    index.by_relation["resolves"] = CountedPositions(index.by_relation["resolves"], "relation")
    assert graph.edges_between(root, None, "2024-05-01", relation="resolves", entity="target") == []
    out = graph.edges_between(root, None, "2024-07-01", relation="resolves", entity="target")
    assert len(out) == 1
    assert out[0].source == "target"
    assert out[0].relation == "resolves"
    assert visited == {"entity": 0, "relation": 2}
