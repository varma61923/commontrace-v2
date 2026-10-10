"""The Neo4j/FalkorDB mirror's CLI doors: backend selection, `graph mirror` and `graph query --backend mirror`."""
from __future__ import annotations

import argparse

import pytest

from commontrace import graph, graph_backends
from commontrace.commands import graph_cmd


class FakeMirror:
    """Records what the CLI asks of a mirror; reads answer from the published snapshot."""

    def __init__(self):
        self.nodes, self.edges, self.closed, self.calls = {}, [], False, []

    def begin_snapshot(self, source_id):
        self.nodes, self.edges = {}, []

    def write_node(self, node_id, properties):
        self.nodes[node_id] = properties

    def write_edge(self, source, target, *, relation="relates_to", **_):
        self.edges.append((source, target, relation))

    def publish_snapshot(self):
        self.calls.append("publish")

    def typed_neighbors(self, node_id, *, relations=None, direction="both", as_of=None, known_at=None):
        self.calls.append(("typed_neighbors", node_id, relations, as_of))
        return [{"neighbor_id": t, "relation": r, "direction": "out"} for s, t, r in self.edges
                if s == node_id and (relations is None or r in relations)]

    def multi_hop(self, start, max_hops=2, relations=None, as_of=None, known_at=None):
        self.calls.append(("multi_hop", tuple(start), max_hops))
        return {"nodes": list(start), "edges": [], "hop_distances": dict.fromkeys(start, 0)}

    def close(self):
        self.closed = True


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path)
    graph.add_node(root, "service:api", "service", "api")
    graph.add_node(root, "service:db", "service", "db")
    graph.add_node(root, "team:core", "team", "core")
    graph.add_edge(root, "service:api", "service:db", "depends_on")
    graph.add_edge(root, "service:api", "team:core", "owned_by")
    return root


def _args(root, **kw):
    base = {"entity": "service:api", "hops": 1, "as_of": "", "known_at": "", "backend": "mirror",
            "relation": [], "dest": root, "json": False}
    return argparse.Namespace(**{**base, **kw})


def test_backend_selection_from_the_environment(monkeypatch):
    monkeypatch.delenv("COMMONTRACE_GRAPH_BACKEND", raising=False)
    assert graph_backends.from_env() is None
    assert graph_backends.from_env("local") is None
    with pytest.raises(ValueError, match="one of neo4j, falkordb"):
        graph_backends.from_env("memgraph")
    monkeypatch.delenv("COMMONTRACE_NEO4J_URI", raising=False)
    with pytest.raises(ValueError, match="COMMONTRACE_NEO4J_URI"):
        graph_backends.from_env("neo4j")
    monkeypatch.setenv("COMMONTRACE_FALKORDB_PORT", "not-a-port")
    with pytest.raises(ValueError, match="port number"):
        graph_backends.from_env("falkordb")


def test_neo4j_settings_reach_the_driver_and_the_password_reads_from_a_file(monkeypatch, tmp_path):
    seen = {}

    class Neo4j:
        def __init__(self, uri, *, username, password, database, namespace):
            seen.update(uri=uri, username=username, password=password, database=database, namespace=namespace)

    secret = tmp_path / "pw"
    secret.write_text("s3cret\n")
    monkeypatch.setattr(graph_backends, "Neo4jGraph", Neo4j)
    monkeypatch.setenv("COMMONTRACE_GRAPH_BACKEND", "neo4j")
    monkeypatch.setenv("COMMONTRACE_NEO4J_URI", "bolt://graph:7687")
    monkeypatch.setenv("COMMONTRACE_NEO4J_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("COMMONTRACE_GRAPH_NAMESPACE", "fleet-a")
    assert isinstance(graph_backends.from_env(), Neo4j)
    assert seen == {"uri": "bolt://graph:7687", "username": "neo4j", "password": "s3cret", "database": "neo4j",
                    "namespace": "fleet-a"}


def test_graph_mirror_publishes_the_local_graph(store, monkeypatch, capsys):
    mirror = FakeMirror()
    monkeypatch.setattr(graph_backends, "from_env", lambda name=None: mirror)
    assert graph_cmd.run_mirror(_args(store, backend=None)) == 0
    assert set(mirror.nodes) == {"service:api", "service:db", "team:core"}
    assert sorted(mirror.edges) == [("service:api", "service:db", "depends_on"),
                                    ("service:api", "team:core", "owned_by")]
    assert mirror.calls == ["publish"] and mirror.closed
    assert "mirrored 3 node(s) and 2 edge(s)" in capsys.readouterr().out


def test_query_reads_through_the_mirror_with_relation_filters(store, monkeypatch, capsys):
    mirror = FakeMirror()
    monkeypatch.setattr(graph_backends, "from_env", lambda name=None: mirror)
    graph_cmd.run_mirror(_args(store, backend=None))
    capsys.readouterr()
    assert graph_cmd.run_query(_args(store, relation=["depends_on"], as_of="2026-01-01")) == 0
    out = capsys.readouterr().out
    assert "--[depends_on]--> (service:db)" in out and "owned_by" not in out
    assert ("typed_neighbors", "service:api", ["depends_on"], "2026-01-01") in mirror.calls
    assert graph_cmd.run_query(_args(store, hops=2)) == 0
    assert ("multi_hop", ("service:api",), 2) in mirror.calls and mirror.closed


def test_unconfigured_mirror_and_misused_relation_fail_clearly(store, monkeypatch, capsys):
    monkeypatch.delenv("COMMONTRACE_GRAPH_BACKEND", raising=False)
    assert graph_cmd.run_mirror(_args(store, backend=None)) == 2
    assert "COMMONTRACE_GRAPH_BACKEND" in capsys.readouterr().err
    assert graph_cmd.run_query(_args(store, backend="local", relation=["depends_on"])) == 1
    assert "--backend mirror" in capsys.readouterr().err
    assert graph_cmd.run_query(_args(store, backend="local")) == 0  # the local path is unchanged
    assert "service:db" in capsys.readouterr().out
