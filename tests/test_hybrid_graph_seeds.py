"""First-pass graph expansion respects semantic recall and canonical admission."""
from __future__ import annotations

import os

from commontrace import fact_store, hierarchical, memory_authority, paths, search_recipes


class Neighbors:
    def __init__(self, edges):
        self.edges = edges
        self.visited = []

    def neighbors(self, node_id, *, as_of=None):
        self.visited.append((node_id, as_of))
        return [{"neighbor_id": identity} for identity in self.edges.get(node_id, ())]


def test_dense_only_hit_seeds_graph_support_without_whole_bank_scan(tmp_path, monkeypatch):
    root = str(tmp_path)
    fact_store.migrate(root)
    anchor, _ = hierarchical.add_fact(root, "Grammar boundaries need validation", scopes=["agent:a"])
    support, _ = hierarchical.add_fact(root, "Use a bounded syntax tree before execution", scopes=["agent:a"])
    private, _ = hierarchical.add_fact(root, "Hidden private account details", scopes=["agent:b"])
    backend = Neighbors({"memory:" + anchor.id: ["memory:" + support.id, "memory:" + private.id]})

    def no_scan(*args, **kwargs):
        raise AssertionError("hybrid graph expansion must not enumerate the fact bank")

    monkeypatch.setattr(hierarchical, "list_facts", no_scan)
    report = {}
    rows = search_recipes.search(root, "parser", context=["agent:a"], embedder="none", reranker="none",
        vector_search=lambda query, limit: [(anchor.id, .9)], backend=backend, explain=report)
    assert {row["id"] for row in rows} == {anchor.id, support.id}
    assert "memory:" + anchor.id in report["graph_seeds"]
    assert report["graph_candidates"] == 1
    assert "graph" not in next(row for row in rows if row["id"] == anchor.id)["signals"]
    found = next(row for row in rows if row["id"] == support.id)
    assert found["distance"] == 1 and found["signals"]["graph"] > 0


def test_rejected_action_candidate_cannot_seed_a_graph_hit(tmp_path):
    root = str(tmp_path)
    fact_store.migrate(root)
    with memory_authority.writer("web", "external"):
        rejected, _ = hierarchical.add_fact(root, "parser pytest public article")
    allowed, _ = hierarchical.add_fact(root, "Use a bounded syntax tree before execution")
    with open(os.path.join(paths.memory_dir(root), "authority-policy.yaml"), "w", encoding="utf-8") as target:
        target.write("actions:\n  deploy: operator\n")
    backend = Neighbors({"memory:" + rejected.id: ["memory:" + allowed.id]})
    rows = search_recipes.search(root, "parser pytest", action_class="deploy", backend=backend,
        embedder="none", reranker="none")
    assert rows == []
    assert backend.visited == []


def test_dense_and_sparse_seeds_are_deduplicated_and_capped(tmp_path, monkeypatch):
    root = str(tmp_path)
    fact_store.migrate(root)
    monkeypatch.setenv("COMMONTRACE_FACT_DEDUP", "exact")
    facts = [hierarchical.add_fact(root, "parser grammar component " + str(index))[0]
             for index in range(12)]
    backend = Neighbors({})
    report = {}
    rows = search_recipes.search(root, "parser", dense_scores={fact.id: .9 for fact in facts},
        backend=backend, embedder="none", reranker="none", explain=report)
    assert rows
    seeds = report["graph_seeds"]
    assert len(seeds) == len(set(seeds)) == 8
    assert [node for node, _ in backend.visited] == seeds


def test_graph_cannot_walk_through_an_out_of_scope_memory(tmp_path):
    root = str(tmp_path)
    fact_store.migrate(root)
    anchor, _ = hierarchical.add_fact(root, "parser validates incoming grammar", scopes=["agent:a"])
    bridge, _ = hierarchical.add_fact(root, "Hidden private account details", scopes=["agent:b"])
    hidden_target, _ = hierarchical.add_fact(root, "Use a bounded syntax tree before execution", scopes=["agent:a"])
    backend = Neighbors({"memory:" + anchor.id: ["memory:" + bridge.id],
                         "memory:" + bridge.id: ["memory:" + hidden_target.id]})
    rows = search_recipes.search(root, "parser", context=["agent:a"], backend=backend,
        embedder="none", reranker="none")
    assert [row["id"] for row in rows] == [anchor.id]
    assert ("memory:" + bridge.id, None) not in backend.visited


def test_lexical_hit_invalidated_during_dense_lookup_cannot_seed(tmp_path):
    root = str(tmp_path)
    fact_store.migrate(root)
    anchor, _ = hierarchical.add_fact(root, "parser validates incoming grammar", valid_from="2024-01-01T00:00:00Z")
    support, _ = hierarchical.add_fact(root, "Use a bounded syntax tree before execution")
    backend = Neighbors({"memory:" + anchor.id: ["memory:" + support.id]})

    def vector_search(query, limit):
        hierarchical.invalidate_fact(root, anchor.id)
        return []

    rows = search_recipes.search(root, "parser", backend=backend, vector_search=vector_search,
        embedder="none", reranker="none")
    assert rows == []
    assert backend.visited == []
