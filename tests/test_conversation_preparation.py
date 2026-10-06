"""Local embedding work is incremental, restartable and shared across requests."""
import concurrent.futures
import threading

import pytest

from commontrace.conversation import Store, embed


@pytest.fixture
def encoder(monkeypatch):
    np = pytest.importorskip("numpy")
    calls = []

    def encode(self, texts, query=False):
        calls.append(list(texts))
        return np.array([[1, len(text) / 1000] for text in texts], dtype=np.float32)

    monkeypatch.setattr(embed.Embedder, "encode", encode)
    return np, calls


def test_prepared_vectors_survive_process_cache_eviction_and_only_new_content_is_encoded(tmp_path, encoder):
    np, calls = encoder
    root = str(tmp_path)
    with Store(root, "prepared") as store:
        store.add("s", [{"text": "Launch planned for Oslo."}])
        e = embed.Embedder(root, "minilm")
        assert embed.prepare(store, e)["units"] == 1
        e.close()
    with Store(root, "prepared") as store:
        e = embed.Embedder(root, "minilm")
        embed.forget_store(store)
        assert embed.search(store, e, np.array([1, 0], dtype=np.float32), 1)
        assert len(calls) == 1  # no corpus encoding during the first question
        embed.prepare(store, e)
        assert len(calls) == 1
        store.add("s", [{"text": "Venue confirmed for Paris."}])
        embed.prepare(store, e)
        assert len(calls) == 2 and len(calls[-1]) == 1
        e.close()


def test_preparation_can_target_sessions_and_rejects_frozen_cache(tmp_path, encoder):
    _np, calls = encoder
    root = str(tmp_path)
    with Store(root, "scoped") as store:
        store.add("work", [{"text": "Venue confirmed for Oslo."}])
        store.add("private", [{"text": "Weekend picnic in Paris."}])
        e = embed.Embedder(root, "minilm")
        assert embed.prepare(store, e, sessions=("work",))["units"] == 1
        assert calls == [["user: Venue confirmed for Oslo."]]
        e.close()
        frozen = embed.Embedder(root, "minilm", read_only=True)
        with pytest.raises(ValueError, match="writable"):
            embed.prepare(store, frozen)
        frozen.close()


def test_overlapping_embedding_requests_encode_once_across_connections(tmp_path, encoder):
    np, calls = encoder
    root = str(tmp_path)
    encoders = [embed.Embedder(root, "minilm") for _ in range(4)]
    ready = threading.Barrier(4)
    items = [("first", "the first passage"), ("second", "another passage")]

    def work(e):
        ready.wait(timeout=5)
        return e.vectors(items)

    try:
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(work, e) for e in encoders]
            results = [f.result(timeout=10) for f in futures]
        assert len(calls) == 1
        for result in results[1:]:
            np.testing.assert_array_equal(results[0], result)
    finally:
        for e in encoders:
            e.close()


def test_failed_preparation_can_resume_without_reencoding_completed_batches(tmp_path, encoder, monkeypatch):
    np, calls = encoder
    root = str(tmp_path)
    successful = embed.Embedder.encode
    monkeypatch.setattr(embed, "SCAN_BATCH", 2)

    def fail(self, texts, query=False):
        if len(calls) == 1:
            raise RuntimeError("local encoder stopped")
        return successful(self, texts, query)

    monkeypatch.setattr(embed.Embedder, "encode", fail)
    with Store(root, "resume") as store:
        store.add("s", [{"text": f"unique passage {i}"} for i in range(5)])
        e = embed.Embedder(root, "minilm")
        with pytest.raises(RuntimeError, match="stopped"):
            embed.prepare(store, e)
        assert e.db.execute("SELECT COUNT(*) FROM vec").fetchone()[0] == 2
        monkeypatch.setattr(embed.Embedder, "encode", successful)
        assert embed.prepare(store, e)["units"] == 5
        assert sum(len(batch) for batch in calls) == 5
        e.close()


def test_index_cli_prepares_local_vectors_and_disabled_embedder_fails_clearly(tmp_path, encoder, monkeypatch, capsys):
    from commontrace.cli import main

    root = str(tmp_path)
    with Store(root, "cli") as store:
        store.add("s", [{"text": "The venue is Oslo."}])
    monkeypatch.setattr(embed, "available", lambda: True)
    assert main(["conversation", "index", "cli", "--model", "minilm", "--json", "--dest", root]) == 0
    assert '"units": 1' in capsys.readouterr().out
    monkeypatch.setenv("COMMONTRACE_CONVERSATION_EMBEDDER", "none")
    assert main(["conversation", "index", "cli", "--dest", root]) == 2
    assert "enabled embedder" in capsys.readouterr().err
