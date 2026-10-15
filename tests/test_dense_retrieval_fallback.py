import contextlib
import sys
import types

import pytest

from commontrace import fact_embeddings, fact_store, fact_vector_projection, hierarchical, search_recipes


class LocalModelUnavailable(Exception):
    """Backend-specific local model error not covered by common built-ins."""


def _stub_local_model_runtime(monkeypatch, constructor):
    sentence_transformers = types.ModuleType("sentence_transformers")
    sentence_transformers.SentenceTransformer = constructor
    monkeypatch.setitem(sys.modules, "sentence_transformers", sentence_transformers)
    from commontrace import rerank_arm
    from commontrace.conversation import embed

    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(rerank_arm, "_no_progress_bars", contextlib.nullcontext)


def test_local_dense_backend_failure_preserves_lexical_results(tmp_path, monkeypatch):
    root = str(tmp_path)
    fact_store.migrate(root)
    expected, _ = hierarchical.add_fact(
        root,
        "The Kubernetes migration reconciles deployment checks before release.",
    )

    def unavailable(*_args, **_kwargs):
        raise LocalModelUnavailable("backend-specific failure details stay private")

    monkeypatch.setattr(fact_vector_projection, "search", unavailable)
    monkeypatch.setattr(fact_embeddings, "scores", unavailable)
    explanation = {}

    rows = search_recipes.search(
        root,
        "Kubernetes migration deployment checks",
        embedder="test-local-model",
        reranker="none",
        explain=explanation,
    )

    result = next(row for row in rows if row["id"] == expected.id)
    assert result["signals"]["bm25"] > 0
    assert "dense" not in result["signals"]
    assert explanation["dense_fallback"] == "LocalModelUnavailable"
    assert "backend-specific failure details" not in str(explanation)


def test_offline_missing_local_model_skips_download_and_searches_lexically(tmp_path, monkeypatch):
    root = str(tmp_path)
    fact_store.migrate(root)
    expected, _ = hierarchical.add_fact(
        root,
        "The Kubernetes migration reconciles deployment checks before release.",
    )
    monkeypatch.setenv("COMMONTRACE_OFFLINE", "1")
    calls = []

    def sentence_transformer(name, **kwargs):
        calls.append((name, kwargs.get("local_files_only", False)))
        if kwargs.get("local_files_only"):
            raise OSError("model cache miss")
        raise AssertionError("offline loading must not try the online download fallback")

    _stub_local_model_runtime(monkeypatch, sentence_transformer)
    explanation = {}
    rows = search_recipes.search(
        root,
        "Kubernetes migration deployment checks",
        embedder="local:fixture/f3-offline-missing",
        reranker="none",
        explain=explanation,
    )

    result = next(row for row in rows if row["id"] == expected.id)
    assert calls == [("fixture/f3-offline-missing", True)]
    assert result["signals"]["bm25"] > 0
    assert "dense" not in result["signals"]
    assert explanation["dense_fallback"] == "OfflineError"


def test_online_local_model_load_keeps_existing_download_retry(monkeypatch):
    monkeypatch.delenv("COMMONTRACE_OFFLINE", raising=False)
    calls = []
    model = types.SimpleNamespace()

    def sentence_transformer(name, **kwargs):
        calls.append((name, kwargs.get("local_files_only", False)))
        if kwargs.get("local_files_only"):
            raise OSError("model cache miss")
        return model

    _stub_local_model_runtime(monkeypatch, sentence_transformer)
    loaded = fact_embeddings._Adapter("local:fixture/f3-online-retry").provider._load()

    assert loaded is model
    assert calls == [("fixture/f3-online-retry", True), ("fixture/f3-online-retry", False)]


def _stub_cross_encoder_runtime(monkeypatch, constructor):
    from commontrace import rerank_arm

    runtime = types.ModuleType("sentence_transformers")
    runtime.CrossEncoder = constructor
    monkeypatch.setitem(sys.modules, "sentence_transformers", runtime)
    monkeypatch.setattr(rerank_arm, "available", lambda: True)
    monkeypatch.setattr(rerank_arm, "_no_progress_bars", contextlib.nullcontext)
    monkeypatch.setattr(rerank_arm, "_LOADED", {})
    monkeypatch.setattr(rerank_arm, "_USE_WORKER", False)
    return rerank_arm


def test_offline_uncached_reranker_preserves_first_stage_without_download(tmp_path, monkeypatch):
    root = str(tmp_path)
    fact_store.migrate(root)
    hierarchical.add_fact(root, "Kubernetes deployment checks reconcile migration manifests.")
    latest, _ = hierarchical.add_fact(root, "Kubernetes deployment checks validate rollout health.")
    reference = latest.created_at
    baseline = search_recipes.search(root, "Kubernetes deployment checks", embedder="none", reranker="none",
                                     as_of=reference)
    assert len(baseline) == 2
    monkeypatch.setenv("COMMONTRACE_OFFLINE", "1")
    calls = []

    def cross_encoder(name, **kwargs):
        calls.append((name, kwargs.get("local_files_only", False)))
        if kwargs.get("local_files_only"):
            raise OSError("model cache miss")
        raise AssertionError("offline reranking must not try to download model weights")

    rerank_arm = _stub_cross_encoder_runtime(monkeypatch, cross_encoder)
    report = {}
    rows = search_recipes.search(root, "Kubernetes deployment checks", embedder="none",
                                 reranker="cross-encoder-fast", explain=report, as_of=reference)

    assert calls == [(rerank_arm.MODELS["cross-encoder-fast"][0], True)]
    assert rows == baseline
    assert report["rerank"]["error"].startswith("OfflineError:")


@pytest.mark.parametrize("use_worker", [False, True])
def test_offline_cached_reranker_remains_available(monkeypatch, use_worker):
    monkeypatch.setenv("COMMONTRACE_OFFLINE", "1")
    calls = []
    model = types.SimpleNamespace(predict=lambda pairs, **_kwargs: [float(i) for i in range(len(pairs))])

    def cross_encoder(name, **kwargs):
        calls.append((name, kwargs.get("local_files_only", False)))
        return model

    rerank_arm = _stub_cross_encoder_runtime(monkeypatch, cross_encoder)
    monkeypatch.setattr(rerank_arm, "_USE_WORKER", use_worker)
    from commontrace import warm

    def online_worker(*_args):
        raise AssertionError("an existing worker may have inherited an online environment")

    monkeypatch.setattr(warm, "rerank_scores", online_worker)
    rows, _ = rerank_arm.rerank("question", ["a", "b"], {"a": "one", "b": "two"}, 2,
                               mode="cross-encoder-fast")
    assert [identity for identity, _score in rows] == ["b", "a"]
    assert rerank_arm._load("cross-encoder-fast") is model
    assert calls == [(rerank_arm.MODELS["cross-encoder-fast"][0], True)]


def test_online_reranker_keeps_download_retry(monkeypatch):
    monkeypatch.delenv("COMMONTRACE_OFFLINE", raising=False)
    calls = []
    model = types.SimpleNamespace()

    def cross_encoder(name, **kwargs):
        calls.append((name, kwargs.get("local_files_only", False)))
        if kwargs.get("local_files_only"):
            raise OSError("model cache miss")
        return model

    rerank_arm = _stub_cross_encoder_runtime(monkeypatch, cross_encoder)
    assert rerank_arm._load("cross-encoder-fast") is model
    name = rerank_arm.MODELS["cross-encoder-fast"][0]
    assert calls == [(name, True), (name, False)]
