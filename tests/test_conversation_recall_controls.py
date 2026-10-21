"""Advanced recall features remain reachable through the public HTTP contract."""
from __future__ import annotations

import json

import pytest

from unimem import gateway, providers
from unimem.conversation import Store
from unimem.conversation.search import _rerank


def call(app, body):
    response = app.handle("POST", "/v1/conversation/recall", {"Authorization": "Bearer controls"},
                          json.dumps({"space": "project", "question": "Orion", **body}).encode())
    return response.status, json.loads(response.body)


@pytest.mark.parametrize("key,value", [
    ("graph_hops", True), ("graph_hops", 3), ("graph_hops", -1), ("rerank_depth", 0),
    ("rerank_depth", 201), ("rerank_depth", 1.5), ("second_pass", "true"), ("summaries", 1),
    ("entity_boost", -0.1), ("entity_boost", True), ("lexical_weight", 2),
    ("rerank_blend", float("nan")), ("rerank_blend", float("inf")), ("rerank_blend", 10**400),
    ("embedder", "os.system"), ("embedder", {}), ("rerank", "os.system"),
    ("rerank", "cohere:bad model"), ("rerank", "mmr:model"),
    ("since", False), ("until", 2025), ("now", []), ("since", "x" * 129),
    ("sessions", ""), ("speakers", False),
])
def test_invalid_controls_fail_before_opening_a_store(tmp_path, key, value):
    app = gateway.Gateway(str(tmp_path), token="controls")
    try:
        assert call(app, {key: value})[0] == 400
        assert not (tmp_path / "memory" / "conversations").exists()
    finally:
        app.close()


def test_graph_depth_and_source_scope_are_honored_over_http(tmp_path):
    with Store(str(tmp_path), "project") as store:
        store.add("work", [{"text": "My colleague Mira leads Project Zephyr."}])
        store.add("award", [{"text": "Mira won the Polaris Prize."}])
    app = gateway.Gateway(str(tmp_path), token="controls")
    body = {"question": "What award did my colleague who leads Project Zephyr receive?",
            "embedder": None, "rerank": None, "summaries": False}
    try:
        status, disabled = call(app, {**body, "graph_hops": 0})
        assert status == 200 and "Polaris" not in disabled["context"]
        status, enabled = call(app, {**body, "graph_hops": 2})
        assert status == 200 and "Polaris" in enabled["context"]
        assert enabled["explain"]["graph_paths"]
        status, scoped = call(app, {**body, "graph_hops": 2, "sessions": ["work"]})
        assert status == 200 and "Polaris" not in scoped["context"]
    finally:
        app.close()


def test_mmr_diversifies_conversation_head_and_preserves_tail(tmp_path):
    with Store(str(tmp_path), "project") as store:
        store.add("work", [{"text": "Retry the upload with exponential backoff jitter."},
                           {"text": "Retry upload exponential backoff with jitter."},
                           {"text": "Use an idempotency key for payment requests."},
                           {"text": "Record deployment logs."}])
        ids = [row[0] for row in store.db.execute("SELECT id FROM turns ORDER BY id")]
        report = {}
        ranked = _rerank(store, "upload", ids, "mmr", 3, report)
        assert ranked == [ids[0], ids[2], ids[1], ids[3]]
        assert report["rerank_stage"]["moved"] == 2


def test_registered_reranker_options_reach_real_http_ranking(tmp_path, monkeypatch):
    from unimem.reranking import Hit

    seen = []

    class Reverse:
        name = "fixture-controls"

        def rerank(self, query, documents, top_n=None):
            seen.append((query, list(documents), top_n))
            return [Hit(i, float(i)) for i in reversed(range(len(documents)))]

    monkeypatch.setitem(providers.RERANKERS._factories, "fixture-controls", Reverse)
    with Store(str(tmp_path), "project") as store:
        store.add("work", [{"text": "Orion invoices use PostgreSQL."},
                           {"text": "Orion invoices are archived in Oslo."}])
    app = gateway.Gateway(str(tmp_path), token="controls")
    try:
        status, result = call(app, {"embedder": "none", "rerank": "fixture-controls", "rerank_depth": 2,
                                    "rerank_blend": 0, "second_pass": True, "entity_boost": 0,
                                    "lexical_weight": 1, "graph_hops": 0, "summaries": False})
        assert status == 200 and seen and seen[0][2] == 2
        assert result["explain"]["rerank_stage"]["reranker"] == "fixture-controls"
        assert "Oslo" in result["context"]
    finally:
        app.close()


def test_temporal_response_matches_generated_array_contract(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    from tests.test_gateway_openapi_contract import _json_schema

    with Store(str(tmp_path), "project") as store:
        store.add("work", [{"text": "Orion launch completed."}], session_at="2025-01-10")
    app = gateway.Gateway(str(tmp_path), token="controls")
    try:
        status, result = call(app, {"question": "What happened in January 2025?", "now": "2025-02-01",
                                    "embedder": None, "rerank": None})
        assert status == 200 and len(result["window"]) == 3
        schema = app._openapi({}, {})["paths"]["/v1/conversation/recall"]["post"]["responses"]["200"][
            "content"]["application/json"]["schema"]
        jsonschema.Draft4Validator(_json_schema(schema)).validate(result)
    finally:
        app.close()


def test_http_semantic_recall_reuses_vectors_prepared_during_capture(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    from unimem import embeddings

    calls = []

    class Encoder:
        def __init__(self, spec):
            self.spec = spec

        def embed(self, texts, *, query):
            calls.append((query, list(texts)))
            return [[1.0] + [0.0] * 31 for _ in texts]

    monkeypatch.setitem(embeddings._CUSTOM, "fixturecontrols", Encoder)
    tag = "fixturecontrols:dense@32"
    with Store(str(tmp_path), "project") as store:
        added = store.add("work", [{"text": "The headquarters is in Tokyo."}],
                          cache_vectors=True, embedder=tag)
        assert added["embedding_cache"]["ready"]
    prepared = sum(len(texts) for query, texts in calls if not query)
    app = gateway.Gateway(str(tmp_path), token="controls")
    try:
        status, result = call(app, {"question": "Where is the main office?", "embedder": tag,
                                    "rerank": None, "graph_hops": 0, "summaries": False})
        assert status == 200 and "Tokyo" in result["context"]
        assert any(query for query, _texts in calls)
        assert sum(len(texts) for query, texts in calls if not query) == prepared
        assert "dense_fallback" not in result["explain"]
    finally:
        app.close()
