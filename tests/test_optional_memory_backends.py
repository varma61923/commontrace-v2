"""Real optional engines: staged visibility, owner identity and canonical admission."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types
import uuid

import pytest


@pytest.mark.parametrize("engine", ["neo4j", "falkor"])
def test_graph_native_snapshot_owner_and_stale_publication(engine):
    if os.environ.get("COMMONTRACE_TEST_GRAPH_BACKENDS") != "1":
        pytest.skip("requires disposable native Neo4j and FalkorDB services")
    from commontrace.graph_backends import FalkorGraph, Neo4jGraph

    namespace = uuid.uuid4().hex
    def create():
        if engine == "falkor":
            return FalkorGraph(port=int(os.environ.get("COMMONTRACE_TEST_FALKOR_PORT", "6379")),
                               graph_name="commontrace_test", namespace=namespace)
        return Neo4jGraph(os.environ.get("COMMONTRACE_TEST_NEO4J_URI", "bolt://127.0.0.1:7687"),
                          username="neo4j", password="commontrace-native-test", namespace=namespace)
    first, stale = create(), create()
    try:
        first.begin_snapshot("source")
        stale.begin_snapshot("source")
        first.write_node("a", {})
        first.write_node("b", {})
        first.write_edge("a", "b", weight=.5, valid_from="2025-01-01T00:00:00Z",
                         valid_until="2027-01-01T00:00:00Z")
        assert not first.neighbors("a", as_of="2026-01-01")  # Unpublished staging stays invisible.
        first.publish_snapshot()
        with pytest.raises(ValueError, match="begin"):
            first.write_node("mutating-visible-snapshot", {})
        assert first.neighbors("a", as_of="2026-01-01") == [{"neighbor_id": "b", "weight": .5}]
        assert not first.neighbors("a", as_of="2024-01-01")
        assert not first.neighbors("a", as_of="2027-01-01")
        with pytest.raises(RuntimeError, match="conflicted"):
            stale.publish_snapshot()
        stale.begin_snapshot("source")
        stale.write_node("a", {})
        stale.publish_snapshot()
        assert not first.neighbors("a", as_of="2026-01-01")
        with pytest.raises(PermissionError, match="different canonical source"):
            first.begin_snapshot("another-source")
    finally:
        first.close()
        stale.close()


def _bitemporal_store(tmp_path):
    """A local graph with an exclusive relation closed later in record time."""
    from commontrace import graph

    (tmp_path / "memory").mkdir(exist_ok=True)
    root = str(tmp_path)
    graph.add_edge(root, "person:ada", "organization:acme", "works_at", valid_at="2020-01-01T00:00:00Z")
    graph.add_edge(root, "organization:globex", "place:berlin", "located_in", valid_at="2019-01-01T00:00:00Z")
    graph.add_edge(root, "person:ada", "tool:git", "uses", valid_at="2018-01-01T00:00:00Z")
    known_before = graph._now()
    time.sleep(0.01)
    graph.add_edge(root, "person:ada", "organization:globex", "works_at", valid_at="2023-01-01T00:00:00Z")
    return root, known_before


@pytest.mark.parametrize("engine", ["neo4j", "falkor"])
def test_graph_native_typed_bitemporal_parity(engine, tmp_path):
    if os.environ.get("COMMONTRACE_TEST_GRAPH_BACKENDS") != "1":
        pytest.skip("requires disposable native Neo4j and FalkorDB services")
    from commontrace import graph
    from commontrace.graph_backends import FalkorGraph, Neo4jGraph, rebuild

    root, known_before = _bitemporal_store(tmp_path)
    namespace = uuid.uuid4().hex
    if engine == "falkor":
        backend = FalkorGraph(port=int(os.environ.get("COMMONTRACE_TEST_FALKOR_PORT", "6379")),
                              graph_name="commontrace_test", namespace=namespace)
    else:
        backend = Neo4jGraph(os.environ.get("COMMONTRACE_TEST_NEO4J_URI", "bolt://127.0.0.1:7687"),
                             username="neo4j", password="commontrace-native-test", namespace=namespace)
    try:
        rebuild(root, backend)
        rows = backend._query("MATCH (a:CommonTrace {namespace:$ns})-[e]->() WHERE e.relation='works_at' "
                              "RETURN DISTINCT type(e)", {"ns": namespace})
        assert rows == [["WORKS_AT"]]
        for as_of, known_at in (("2021-06-01", None), ("2024-01-01", None), ("2024-01-01", known_before),
                                (None, None), (None, known_before)):
            local = {(n["neighbor_id"], n["relation"], n["direction"])
                     for n in graph.get_neighbors(root, "person:ada", as_of=as_of, known_at=known_at)}
            native = {(n["neighbor_id"], n["relation"], n["direction"])
                      for n in backend.typed_neighbors("person:ada", as_of=as_of, known_at=known_at)}
            assert native == local, (as_of, known_at)
            local_hops = graph.multi_hop_subgraph(root, ["person:ada"], max_hops=2, as_of=as_of,
                                                  known_at=known_at)["hop_distances"]
            assert backend.multi_hop("person:ada", 2, as_of=as_of, known_at=known_at)["hop_distances"] == local_hops
        assert {n["neighbor_id"] for n in backend.typed_neighbors("person:ada", relations=["works_at"],
                                                                  as_of="2024-01-01")} == {"organization:globex"}
        assert backend.multi_hop("person:ada", 2, as_of="2024-01-01")["hop_distances"]["place:berlin"] == 2
        assert "place:berlin" not in backend.multi_hop("person:ada", 2, relations=["uses", "works_at"],
                                                       as_of="2021-01-01")["hop_distances"]
    finally:
        backend.close()


# --- fake drivers: the Cypher and parameters the mirror emits ---------------------------

class _Recorder:
    """Answers the snapshot protocol and records every (statement, params)."""

    def __init__(self, rows=None):
        self.calls = []
        self.rows = rows or (lambda statement, params: [])

    def __call__(self, statement, params):
        self.calls.append((statement, dict(params)))
        if statement.startswith("MERGE (s:CommonTraceIndex"):
            return [[params["source"], ""]]
        if "SET s.current=$gen" in statement:
            return [[params["gen"]]]
        if statement == "CALL db.constraints()":
            return [["UNIQUE", "CommonTraceIndex", ["namespace"], "NODE", "OPERATIONAL"]]
        return self.rows(statement, params)

    def edge_writes(self):
        return [(s, p) for s, p in self.calls if "CREATE (a)-[e:" in s]


def _fake_neo4j(monkeypatch, recorder):
    class Record:
        def __init__(self, row):
            self.row = row

        def values(self):
            return list(self.row)

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def run(self, statement, **params):
            return [Record(row) for row in recorder(statement, params)]

    class Driver:
        def session(self, database):
            assert database == "neo4j"
            return Session()

        def close(self):
            pass

    module = types.ModuleType("neo4j")
    module.GraphDatabase = types.SimpleNamespace(driver=lambda uri, auth: Driver())
    monkeypatch.setitem(sys.modules, "neo4j", module)


def _fake_falkor(monkeypatch, recorder):
    class Graph:
        def query(self, statement, params):
            return types.SimpleNamespace(result_set=recorder(statement, params))

    class FalkorDB:
        def __init__(self, host, port, password):
            self.connection = types.SimpleNamespace(execute_command=lambda *args: "OK")

        def select_graph(self, name):
            return Graph()

        def close(self):
            pass

    module = types.ModuleType("falkordb")
    module.FalkorDB = FalkorDB
    monkeypatch.setitem(sys.modules, "falkordb", module)


def test_relationship_type_sanitization():
    from commontrace.graph_backends import GENERIC_TYPE, relation_key, relationship_type

    assert relationship_type("works_at") == "WORKS_AT"
    assert relationship_type("1st_owner") == GENERIC_TYPE  # must start with a letter
    assert relationship_type("a" * 65) == GENERIC_TYPE
    assert relationship_type("x`]->(b) DELETE b") == GENERIC_TYPE
    assert relation_key("Hosted-On") == "hosted_on"
    with pytest.raises(ValueError):
        relation_key("  ")


def test_neo4j_mirror_writes_typed_bitemporal_edges(monkeypatch, tmp_path):
    from commontrace import graph
    from commontrace.graph_backends import Neo4jGraph, rebuild

    root, _known = _bitemporal_store(tmp_path)
    recorder = _Recorder()
    _fake_neo4j(monkeypatch, recorder)
    backend = Neo4jGraph("bolt://fake", username="u", password="p", namespace="ns")
    assert rebuild(root, backend)["edges"] == 4
    writes = recorder.edge_writes()
    assert len(writes) == 4
    closed = next(p for s, p in writes if p["target"] == "organization:acme")
    statement = next(s for s, p in writes if p["target"] == "organization:acme")
    assert "CREATE (a)-[e:`WORKS_AT`]->(b)" in statement
    for prop in ("e.relation=$relation", "e.valid_at=$start", "e.invalid_at=$end", "e.expired_at=$expired",
                 "e.created_at=$created", "e.closed_recorded_at=$closed"):
        assert prop in statement
    edge = next(e for e in graph.load_edges(root) if e.target == "organization:acme")
    assert closed["relation"] == "works_at"
    assert closed["start"] == "2020-01-01T00:00:00.000000+00:00"
    assert closed["end"] == "2023-01-01T00:00:00.000000+00:00" == closed["until"]
    assert closed["closed"] and closed["closed"][:19] == edge.properties["closed_recorded_at"][:19]
    assert closed["created"][:19] == edge.created_at[:19]
    assert json.loads(closed["properties"])["closed_reason"] == "superseded by organization:globex"
    # Ids, relation names and timestamps are parameters, never statement text.
    assert all("person:ada" not in s and "works_at" not in s for s, _ in writes)


def test_untypable_relation_keeps_generic_type_and_property(monkeypatch):
    from commontrace.graph_backends import Neo4jGraph

    recorder = _Recorder()
    _fake_neo4j(monkeypatch, recorder)
    backend = Neo4jGraph("bolt://fake", username="u", password="p", namespace="ns")
    backend.begin_snapshot("src")
    backend.write_edge("a", "b", relation="9lives", valid_from="2025-01-01T00:00:00Z",
                       valid_until="2026-01-01T00:00:00Z")
    statement, params = recorder.edge_writes()[0]
    assert "[e:`MEMORY`]" in statement and params["relation"] == "9lives"
    assert params["start"] == "2025-01-01T00:00:00.000000+00:00"
    assert params["end"] == "2026-01-01T00:00:00.000000+00:00"
    assert params["expired"] is None and params["created"] is None


def test_typed_neighbors_query_is_parameterized_and_bitemporal(monkeypatch):
    from commontrace.graph_backends import ACTIVE_EDGE, FalkorGraph

    def rows(statement, params):
        return [["a", "b", "works_at", True, 0.5, "2020-01-01T00:00:00.000000+00:00", None],
                ["a", "c", "uses", False, 1.0, None, None]]

    recorder = _Recorder(rows)
    _fake_falkor(monkeypatch, recorder)
    backend = FalkorGraph(namespace="ns")
    out = backend.typed_neighbors("a", relations=["Works-At", "uses"], direction="out",
                                  as_of="2024-01-01", known_at="2023-06-01T12:00:00Z")
    statement, params = recorder.calls[-1]
    assert ACTIVE_EDGE in statement and "($relations IS NULL OR e.relation IN $relations)" in statement
    assert "(n:CommonTrace {namespace:$ns})-[e]->(m:CommonTrace {namespace:$ns})" in statement
    assert params["relations"] == ["uses", "works_at"] and params["ids"] == ["a"]
    assert params["at"] == "2024-01-01T00:00:00.000000+00:00"
    assert params["known"] == "2023-06-01T12:00:00.000000+00:00"
    assert out == [{"neighbor_id": "b", "relation": "works_at", "direction": "out", "weight": 0.5,
                    "valid_at": "2020-01-01T00:00:00.000000+00:00", "invalid_at": None},
                   {"neighbor_id": "c", "relation": "uses", "direction": "in", "weight": 1.0,
                    "valid_at": None, "invalid_at": None}]
    with pytest.raises(ValueError, match="direction"):
        backend.typed_neighbors("a", direction="sideways")


def test_visibility_defaults_follow_the_local_graph():
    from commontrace.graph_backends import visibility

    assert visibility(known_at="2024-05-01") == {"at": "2024-05-01T00:00:00.000000+00:00",
                                                 "known": "2024-05-01T00:00:00.000000+00:00"}
    current = visibility()
    assert current["known"] is None and current["at"].endswith("+00:00") and len(current["at"]) == 32


def test_multi_hop_walks_one_bounded_query_per_hop(monkeypatch):
    from commontrace.graph_backends import Neo4jGraph

    edges = [("a", "b", "works_at"), ("b", "c", "located_in"), ("c", "d", "contains"), ("d", "e", "uses")]

    def rows(statement, params):
        out = []
        for source, target, relation in edges:
            if params["relations"] is not None and relation not in params["relations"]:
                continue
            if source in params["ids"]:
                out.append([source, target, relation, True, 1.0, None, None])
            if target in params["ids"]:
                out.append([target, source, relation, False, 1.0, None, None])
        return out[:params["limit"]]

    recorder = _Recorder(rows)
    _fake_neo4j(monkeypatch, recorder)
    backend = Neo4jGraph("bolt://fake", username="u", password="p", namespace="ns")
    sub = backend.multi_hop("a", 3, as_of="2024-01-01")
    assert sub["hop_distances"] == {"a": 0, "b": 1, "c": 2, "d": 3}
    assert [(e["source"], e["target"]) for e in sub["edges"]] == [("a", "b"), ("b", "c"), ("c", "d")]
    assert len(recorder.calls) == 3 and all(p["at"].startswith("2024-01-01") for _, p in recorder.calls)
    assert backend.multi_hop("a", 4, relations=["works_at"])["hop_distances"] == {"a": 0, "b": 1}
    assert len(backend.multi_hop(["a", "e"], 4, max_edges=2)["edges"]) == 2
    for bad in (0, 5, True, "2"):
        with pytest.raises(ValueError, match="max_hops"):
            backend.multi_hop("a", bad)
    with pytest.raises(ValueError):
        backend.multi_hop([], 2)


def test_lance_native_owner_generation_filter_and_prune(tmp_path):
    if os.environ.get("COMMONTRACE_TEST_LANCE") != "1":
        pytest.skip("requires the optional LanceDB engine")
    from commontrace.vector_lance import LanceVectorIndex
    from commontrace.vector_store import VectorRecord

    async def run():
        index = LanceVectorIndex(str(tmp_path), tenant="alice", namespace="facts", model="test", dimension=2)
        other = LanceVectorIndex(str(tmp_path), tenant="bob", namespace="facts", model="test", dimension=2)
        try:
            await index.bind_source("alice-source")
            with pytest.raises(ValueError, match="different canonical source"):
                await index.bind_source("bob-source")
            await index.upsert([VectorRecord("a", [1., 0.], "g1"), VectorRecord("b", [0., 1.], "g2")])
            assert not await other.search([1., 0.])
            assert not await index.search([1., 0.], allowed_ids=[])
            assert [r.key for r in await index.search([1., 0.], allowed_ids=["a"], generation="g1")] == ["a"]
            assert await index.prune("g2") == 1
            assert [r.key for r in await index.search([0., 1.])] == ["b"]
            assert await index.delete(["b"]) == 1
            assert not await index.search([1., 0.])
        finally:
            await index.close()
            await other.close()
    asyncio.run(run())
