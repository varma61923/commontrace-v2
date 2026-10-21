"""Performance evidence uses a graph attainable through canonical edge writes."""
from __future__ import annotations

from benchmarks import graph_relations
from unimem import graph


def test_fixture_has_distinct_neighbors_and_unique_canonical_edge_keys(monkeypatch):
    saved = {}
    save_nodes, save_edges = graph.save_nodes, graph.save_edges

    def nodes(root, records):
        saved["nodes"] = records
        save_nodes(root, records)

    def edges(root, records):
        saved["edges"] = records
        save_edges(root, records)

    monkeypatch.setattr(graph, "save_nodes", nodes)
    monkeypatch.setattr(graph, "save_edges", edges)
    result = graph_relations.measure(edges=100, trials=1)
    records = saved["edges"]
    keys = {(edge.source, edge.target, edge.relation) for edge in records}
    assert len(keys) == len(records) == 100
    assert len({edge.target for edge in records}) == 100
    assert all(edge.source in saved["nodes"] and edge.target in saved["nodes"] for edge in records)
    assert result["equivalent"] and result["matching_edges"] == 4
