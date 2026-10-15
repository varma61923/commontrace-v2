"""Executable optional retrieval-policy vectors, independent of causal v1."""
from __future__ import annotations

import json
import os

import pytest

from commontrace import fact_store, hierarchical, search_recipes

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(_ROOT, "protocol", "conformance", "retrieval-vectors.json"), encoding="utf-8") as source:
    _VECTORS = json.load(source)


def populate(root, rows):
    fact_store.migrate(root)
    with fact_store.transaction(root, write=True) as facts:
        for identity, metadata in rows.items():
            facts[identity] = hierarchical.AtomicFact(identity, metadata["statement"], "general", metadata["scopes"],
                .8, 1, "2024-01-01T00:00:00Z", None, expires_at=metadata.get("expires_at"),
                created_at="2024-01-01T00:00:00Z", updated_at="2024-01-01T00:00:00Z")


@pytest.mark.parametrize("vector", _VECTORS["graph"], ids=lambda vector: vector["name"])
def test_graph_vector(tmp_path, vector):
    root = str(tmp_path)
    populate(root, vector["facts"])

    class Backend:
        def neighbors(self, node_id, *, as_of=None):
            return [{"neighbor_id": identity} for identity in vector["edges"].get(node_id, [])]

    rows = search_recipes.search(root, "parser", context=vector["context"], dense_scores=vector["dense"],
        backend=Backend(), embedder="none", reranker="none")
    assert sorted(row["id"] for row in rows) == sorted(vector["expected_ids"])


@pytest.mark.parametrize("vector", _VECTORS["multi_round"], ids=lambda vector: vector["name"])
def test_multiround_vector(tmp_path, monkeypatch, vector):
    root = str(tmp_path)
    identities = {identity for rows in vector["searches"].values() for identity in rows}
    populate(root, {identity: {"statement": identity, "scopes": []} for identity in identities})
    observed = []

    def retrieve(root, query, **options):
        observed.append(query)
        return [{"id": identity, "text": identity, "score": 1} for identity in vector["searches"][query]]

    decisions = iter(vector["decisions"])
    monkeypatch.setattr(search_recipes, "search", retrieve)
    results = search_recipes.multi_round(root, vector["question"], limit=vector["limit"],
        complete=lambda prompt: (json.dumps(next(decisions)), {}))
    assert [row["id"] for row in results["results"]] == vector["expected_ids"]
    assert observed == vector["expected_searches"]
