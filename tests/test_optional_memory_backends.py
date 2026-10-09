"""Real optional engines: staged visibility, owner identity and canonical admission."""
from __future__ import annotations

import asyncio
import os
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
