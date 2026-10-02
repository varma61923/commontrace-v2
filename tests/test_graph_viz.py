from __future__ import annotations

import pytest

from commontrace import graph
from commontrace.graph_viz import render_html


@pytest.fixture
def store(tmp_path):
    return str(tmp_path / "vizstore")


def _basic(store):
    graph.add_node(store, "service:auth", "service", "Auth")
    graph.add_node(store, "tool:jwt", "tool", "JWT")
    graph.add_node(store, "memory:m1", "memory", "M1")
    graph.add_edge(store, "service:auth", "tool:jwt", "uses")
    return store


def test_render_synthetic_graph(store):
    _basic(store)
    out = render_html(store)
    assert "<html" in out.lower()
    assert "<svg" in out.lower()
    assert "service:auth" in out
    assert "inspect" in out.lower()
    assert "legend" in out.lower()


def test_edge_vocab_present(store):
    graph.add_node(store, "memory:a", "memory", "A")
    graph.add_node(store, "memory:b", "memory", "B")
    graph.add_node(store, "memory:c", "memory", "C")
    graph.add_node(store, "memory:d", "memory", "D")
    graph.add_edge(store, "memory:b", "memory:a", "updates")
    graph.add_edge(store, "memory:c", "memory:b", "extends")
    graph.add_edge(store, "memory:d", "memory:c", "derives")
    graph.add_edge(store, "memory:d", "memory:a", "supersedes")
    out = render_html(store)
    for rel in ("updates", "extends", "derives", "supersedes"):
        assert rel in out


def test_superseded_dimming_class(store):
    n1 = graph.add_node(store, "memory:evo", "memory", "Evo", properties={"status": "draft"})
    graph.create_memory_version(store, "memory:evo", new_properties={"status": "v2"})
    graph.add_node(store, "service:api", "service", "API")
    graph.add_node(store, "tool:cache", "tool", "Cache")
    graph.add_edge(store, "service:api", "tool:cache", "uses", valid_at="2024-01-01T00:00:00Z")
    graph.add_edge(store, "service:api", "tool:cache", "uses", valid_at="2024-01-02T00:00:00Z")
    out = render_html(store)
    low = out.lower()
    assert "dimmed" in low
    assert "superseded" in low


def test_valid_html_structure_no_cdn(store):
    _basic(store)
    out = render_html(store)
    low = out.lower()
    assert "<!doctype html" in low
    assert "<head" in low and "</head>" in low
    assert "<body" in low and "</body>" in low
    assert "<script" in low
    assert "cdn" not in low
    assert 'src="http' not in low


def test_as_of_filtering(store):
    graph.add_node(store, "service:s", "service", "S")
    graph.add_node(store, "tool:old", "tool", "Old")
    graph.add_node(store, "tool:future", "tool", "Future")
    graph.add_edge(store, "service:s", "tool:old", "uses", valid_at="2024-01-01T00:00:00Z")
    graph.add_edge(
        store, "service:s", "tool:future", "depends_on", valid_at="2030-01-01T00:00:00Z"
    )
    early = render_html(store, as_of="2024-06-01T00:00:00Z")
    late = render_html(store, as_of="2031-01-01T00:00:00Z")
    assert "tool:old" in early
    assert 'data-target="tool:future"' not in early
    assert 'data-target="tool:future"' in late
    assert 'data-target="tool:old"' in early
