"""The optional local default is cache-only, with lexical retrieval as its fallback."""
from __future__ import annotations

import sys
from types import SimpleNamespace

from unimem import embeddings, fact_embeddings, hierarchical, kg_similarity, search_recipes
from unimem.configuration import DEFAULTS
from unimem.conversation import Options, Store, embed, recall, search

DEFAULT_TAG = "fastembed:BAAI/bge-small-en-v1.5"


def test_profiles_and_conversation_choose_the_fastembed_default(monkeypatch):
    assert all(profile["embedding"] == DEFAULT_TAG for profile in DEFAULTS["profiles"].values())
    monkeypatch.delenv("UNIMEM_CONVERSATION_EMBEDDER", raising=False)
    monkeypatch.setattr(embed, "_configuration_getenv", lambda _name, default: default)
    monkeypatch.setattr(embed, "available_for", lambda tag: tag == DEFAULT_TAG)

    assert embed.configured() == DEFAULT_TAG


def test_conversation_cache_miss_keeps_lexical_recall_and_never_downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", DEFAULT_TAG)
    monkeypatch.setattr(embed, "available_for", lambda _tag: True)
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(asarray=lambda rows, **_kw: rows, float32="float32"))
    calls = []

    class CacheMiss:
        def __init__(self, *, local_files_only, **_kwargs):
            calls.append(local_files_only)
            if not local_files_only:
                raise AssertionError("the default model must not download on a cache miss")
            raise FileNotFoundError("model weights are absent")

    monkeypatch.setattr("unimem.embeddings._OnnxText", CacheMiss)
    with Store(str(tmp_path), "project") as store:
        store.add("session", [{"speaker": "user", "text": "The release codename is Kestrel."}])
        result = recall(store, "What is the release codename?", options=Options(
            embedder="auto", rerank=None, summaries=False,
        ))

    assert "Kestrel" in result.context
    assert result.explain["dense_fallback"] == "EmbeddingError"
    assert calls == [True]


def test_fact_search_keeps_lexical_results_without_fastembed(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setattr(fact_embeddings.importlib.util, "find_spec", lambda _name: None)
    fact, _ = hierarchical.add_fact(
        root, "Kubernetes deployment checks reconcile migration manifests before release."
    )

    rows = search_recipes.search(
        root, "Kubernetes deployment checks", reranker="none", limit=5,
    )

    result = next(row for row in rows if row["id"] == fact.id)
    assert result["signals"]["lexical"] > 0
    assert result["signals"]["dense"] == 0


def test_fact_default_provider_is_local_cache_only(monkeypatch):
    monkeypatch.setattr(fact_embeddings.importlib.util, "find_spec", lambda _name: object())
    fact_embeddings._MODELS.pop(("tag", DEFAULT_TAG), None)

    adapter, _key = fact_embeddings._resolve(None, None)

    assert adapter is not None
    assert adapter.provider.local_files_only is True


def test_fact_embedding_disable_setting_does_not_load_the_default(monkeypatch):
    def unexpected_model(_tag):
        raise AssertionError("an explicitly disabled embedder must not load a model")

    monkeypatch.setattr(fact_embeddings, "_tag_model", unexpected_model)
    for setting in ("none", "off", "NONE"):
        monkeypatch.setattr(fact_embeddings, "_configuration_getenv", lambda *_args: setting)
        assert fact_embeddings._resolve(None, None) == (None, None)


def test_graph_default_provider_is_cache_only_but_other_tags_keep_explicit_behavior(monkeypatch):
    calls = []
    providers = [object(), object()]

    def provider(tag, *, local_files_only=False):
        calls.append((tag, local_files_only))
        return providers[len(calls) - 1]

    monkeypatch.setattr(embeddings, "provider", provider)

    assert kg_similarity.embedder(DEFAULT_TAG) is providers[0]
    assert kg_similarity.embedder("fastembed:BAAI/bge-base-en-v1.5") is providers[1]
    assert calls == [(DEFAULT_TAG, True), ("fastembed:BAAI/bge-base-en-v1.5", False)]


def test_auto_rerank_uses_only_a_cached_cross_encoder(tmp_path, monkeypatch):
    from unimem import rerank_arm

    calls = []

    class CacheMiss:
        def __init__(self, _name, *, local_files_only=False, **_kwargs):
            calls.append(local_files_only)
            if not local_files_only:
                raise AssertionError("automatic reranking must not download model weights")
            raise OSError("weights are absent")

    class FakeEmbedder:
        def encode(self, texts, *, query=False):
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=CacheMiss))
    monkeypatch.setattr(rerank_arm, "available", lambda: True)
    monkeypatch.setattr(rerank_arm, "_no_progress_bars", lambda: __import__("contextlib").nullcontext())
    monkeypatch.setattr(rerank_arm, "_LOADED", {})
    monkeypatch.setattr(rerank_arm, "_USE_WORKER", False)
    monkeypatch.setattr(search, "_embedder", lambda _store, _choice: FakeEmbedder())
    monkeypatch.setattr(
        embed, "search_many",
        lambda store, _embedder, vectors, _pool, **_kwargs: [
            [(store.db.execute("SELECT id FROM units ORDER BY id LIMIT 1").fetchone()[0], 1.0)]
            for _ in vectors
        ],
    )

    with Store(str(tmp_path), "project") as store:
        store.add("session", [{"speaker": "user", "text": "The release codename is Kestrel."}])
        result = recall(store, "What is the release codename?", options=Options(
            embedder="auto", rerank="auto", summaries=False,
        ))

    assert "Kestrel" in result.context
    assert result.explain["rerank_fallback"]
    assert result.explain["rerank"] == "cross-encoder-fast"
    assert calls == [True]
