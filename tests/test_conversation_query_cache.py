"""Query vectors may be reused; mutable results, model changes and expired entries must not cross calls."""
import concurrent.futures
import threading

import pytest

from commontrace.conversation import embed


@pytest.fixture
def local_model(monkeypatch):
    np = pytest.importorskip("numpy")

    class Model:
        def __init__(self, offset=0):
            self.calls = []
            self.offset = offset

        def encode(self, texts, **kwargs):
            self.calls.append(list(texts))
            return np.array([[len(text), self.offset] for text in texts], dtype=np.float32)

    model = Model()
    # This fixture injects a local encoder; it does not require the optional
    # sentence-transformers package to be installed in a core-only test run.
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "_QUERY_CACHE", embed.OrderedDict())
    monkeypatch.setattr(embed, "_model", lambda _tag: model)
    return np, model, Model


def test_repeated_queries_share_vectors_across_connections_without_mutable_arrays_or_raw_text(tmp_path, local_model):
    np, model, _cls = local_model
    first = embed.Embedder(str(tmp_path / "one"), "minilm")
    second = embed.Embedder(str(tmp_path / "two"), "minilm")
    try:
        original = first.encode(["alpha query", "beta query", "alpha query"], query=True)
        expected = original.copy()
        original[:] = 999
        actual = second.encode(["beta query", "alpha query"], query=True)
        np.testing.assert_array_equal(actual, expected[[1, 0]])
        assert model.calls == [["alpha query", "beta query"]]
        assert all("alpha query" not in str(key) and "beta query" not in str(key) for key in embed._QUERY_CACHE)
        assert all(not entry[1].flags.writeable for entry in embed._QUERY_CACHE.values())
        first.encode(["alpha query"], query=False)
        assert len(model.calls) == 2  # passage encoding has a different objective
    finally:
        first.close()
        second.close()


def test_query_cache_respects_model_identity_prefix_and_expiration(tmp_path, local_model, monkeypatch):
    np, model, cls = local_model
    clock = [1000]
    monkeypatch.setattr(embed.time, "monotonic", lambda: clock[0])
    e = embed.Embedder(str(tmp_path), "arctic-m")
    try:
        before = e.encode(["same question"], query=True)
        assert model.calls[0][0] == embed.MODELS["arctic-m"][1] + "same question"
        clock[0] += embed.QUERY_CACHE_SECONDS + 1
        e.encode(["same question"], query=True)
        assert len(model.calls) == 2
        replacement = cls(offset=99)
        monkeypatch.setattr(embed, "_model", lambda _tag: replacement)
        after = e.encode(["same question"], query=True)
        assert len(replacement.calls) == 1 and not np.array_equal(before, after)
    finally:
        e.close()


def test_query_cache_is_bounded_and_returns_complete_batch_even_when_evicted(tmp_path, local_model, monkeypatch):
    np, model, _cls = local_model
    monkeypatch.setattr(embed, "QUERY_CACHE_ENTRIES", 2)
    monkeypatch.setattr(embed, "QUERY_CACHE_BYTES", 16)
    e = embed.Embedder(str(tmp_path), "minilm")
    try:
        result = e.encode(["first", "second", "third"], query=True)
        assert len(result) == 3
        assert len(embed._QUERY_CACHE) <= 2
        assert sum(v[1].nbytes for v in embed._QUERY_CACHE.values()) <= 16
        e.encode(["first"], query=True)
        assert len(model.calls) == 2
    finally:
        e.close()


def test_identical_concurrent_query_batches_coalesce_without_deadlock(tmp_path, local_model, monkeypatch):
    np, model, _cls = local_model
    started, release = threading.Event(), threading.Event()
    compute = model.encode

    def delayed(texts, **kwargs):
        started.set()
        assert release.wait(5)
        return compute(texts, **kwargs)

    monkeypatch.setattr(model, "encode", delayed)
    encoders = [embed.Embedder(str(tmp_path), "minilm") for _ in range(4)]
    barrier = threading.Barrier(4)

    def work(index):
        barrier.wait(timeout=5)
        texts = ["first", "second"] if index % 2 == 0 else ["second", "first"]
        return encoders[index].encode(texts, query=True)

    try:
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(work, i) for i in range(4)]
            try:
                assert started.wait(5)
            finally:
                release.set()
            results = [f.result(timeout=10) for f in futures]
        assert len(model.calls) == 1
        np.testing.assert_array_equal(results[0], results[1][::-1])
    finally:
        release.set()
        for e in encoders:
            e.close()


def test_failed_query_encoding_does_not_publish_partial_cache(tmp_path, local_model, monkeypatch):
    np, model, _cls = local_model
    compute = model.encode
    e = embed.Embedder(str(tmp_path), "minilm")
    try:
        monkeypatch.setattr(model, "encode", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("local failure")))
        with pytest.raises(RuntimeError, match="local failure"):
            e.encode(["query"], query=True)
        assert not embed._QUERY_CACHE
        monkeypatch.setattr(model, "encode", compute)
        assert len(e.encode(["query"], query=True)) == 1
    finally:
        e.close()


def test_cached_query_vectors_never_cache_evidence_or_bypass_session_filters_and_updates(tmp_path, local_model):
    from commontrace.conversation import Options, Store, recall

    np, model, _cls = local_model
    question = "Where is Project Zephyr venue?"
    public = Options(embedder="minilm", rerank=None, sessions=("public",), instructions=0, profile_facts=0)
    private = Options(embedder="minilm", rerank=None, sessions=("private",), instructions=0, profile_facts=0)
    with Store(str(tmp_path), "scopes") as store:
        store.add("public", [{"text": "Project Zephyr venue is Oslo."}])
        store.add("private", [{"text": "Project Zephyr venue is Paris."}])
        assert "Oslo" in recall(store, question, options=public).context
        secret = recall(store, question, options=private).context
        assert "Paris" in secret and "Oslo" not in secret
        store.delete_session("public")
        store.add("public", [{"text": "Project Zephyr venue is Rome."}])
        updated = recall(store, question, options=public).context
        assert "Rome" in updated and "Oslo" not in updated and "Paris" not in updated
        assert sum(call.count(question) for call in model.calls) == 1
