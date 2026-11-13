"""Malformed model identifiers never select another turn or abort indexing."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

import pytest

from unimem.conversation import Store, anticipate


@pytest.mark.parametrize("identity", [None, True, 1.9, float("inf"), float("nan"), {}, []])
def test_invalid_identifiers_leave_batch_pending(tmp_path, monkeypatch, identity):
    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", "none")
    with Store(str(tmp_path), "validation") as store:
        store.add("s", [{"speaker": "Ana", "role": "user", "text": "The cat is named Miso."}])

        def complete(_):
            return json.dumps([{"id": identity, "questions": ["What is the cat named?"]}]), {}

        report = anticipate.expand(store, complete)
        assert report["failed_batches"] == 1
        assert report["indexed"] == 0
        assert len(anticipate.pending(store)) == 1


@pytest.mark.parametrize("identity", [1, "1"])
def test_valid_integer_identifiers_remain_supported(tmp_path, monkeypatch, identity):
    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", "none")
    with Store(str(tmp_path), "validation") as store:
        store.add("s", [{"speaker": "Ana", "role": "user", "text": "The cat is named Miso."}])

        def complete(_):
            return json.dumps([{"id": identity, "questions": ["What is the cat named?"]}]), {}

        assert anticipate.expand(store, complete)["indexed"] == 1


@pytest.mark.parametrize("limit", [-1, True, 1.5, "2"])
def test_invalid_limits_do_not_dispatch_model_calls(tmp_path, monkeypatch, limit):
    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", "none")
    with Store(str(tmp_path), "limits") as store:
        store.add("s", [{"speaker": "Ana", "role": "user", "text": "The cat is Miso."}])
        calls = []
        with pytest.raises(ValueError, match="limit"):
            anticipate.expand(store, lambda _: (calls.append(True), {}), limit=limit)
        assert calls == []


def test_deeply_nested_model_json_leaves_the_batch_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", "none")
    with Store(str(tmp_path), "nested") as store:
        store.add("s", [{"speaker": "Ana", "role": "user", "text": "The cat is Miso."}])
        depth = 10_000
        report = anticipate.expand(store, lambda _: ("[" * depth + "]" * depth, {}))
        assert report["failed_batches"] == 1 and report["turns"] == 0
        assert len(anticipate.pending(store)) == 1


@pytest.mark.parametrize("shared_store", [False, True])
def test_overlapping_passes_index_each_turn_once(tmp_path, monkeypatch, shared_store):
    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", "none")
    with Store(str(tmp_path), "overlap") as store:
        store.add("s", [{"speaker": "Ana", "role": "user", "text": "The cat is Miso."}])
        anticipate.pending(store)
    rendezvous = threading.Barrier(2)

    with Store(str(tmp_path), "overlap", create=False) as common, ThreadPoolExecutor(2) as pool:

        def expand():
            manager = nullcontext(common) if shared_store else Store(str(tmp_path), "overlap", create=False)
            with manager as store:

                def complete(_):
                    rendezvous.wait(5)
                    return json.dumps([{"id": 1, "questions": ["feline companion"]}]), {}

                return anticipate.expand(store, complete)

        reports = list(pool.map(lambda _: expand(), range(2)))
    with Store(str(tmp_path), "overlap", create=False) as store:
        count = store.db.execute("SELECT COUNT(*) FROM units WHERE part>=?", (anticipate.EXPANSION_PART,)).fetchone()[0]
        assert count == 1
        assert sum(report["indexed"] for report in reports) == 1
        assert anticipate.pending(store) == []
        if anticipate.FTS5:
            assert store.db.execute("SELECT COUNT(*) FROM units_fts WHERE units_fts MATCH 'feline'").fetchone()[0] == 1


def test_deletion_during_completion_does_not_abort_surviving_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", "none")
    with Store(str(tmp_path), "delete") as store:
        for session in ("removed", "retained"):
            store.add(session, [{"speaker": "Ana", "role": "user", "text": "The cat is Miso."}])

        def complete(_):
            store.delete_session("removed")
            return json.dumps([{"id": i, "questions": ["feline companion"]} for i in (1, 2)]), {}

        report = anticipate.expand(store, complete)
        assert report["turns"] == report["indexed"] == 1
        assert store.db.execute("SELECT COUNT(*) FROM anticipated").fetchone()[0] == 1
        assert anticipate.pending(store) == []


def test_recycled_turn_id_does_not_attach_erased_content_to_a_new_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", "none")
    with Store(str(tmp_path), "replacement") as store:
        store.add("removed", [{"speaker": "Ana", "role": "user", "text": "The cat is Miso."}])
        original_id = next(iter(store.turns(anticipate.pending(store))))

        def complete(_):
            store.delete_session("removed")
            store.add("replacement", [{"speaker": "Ana", "role": "user", "text": "The meeting is Friday."}])
            assert store.db.execute("SELECT id FROM turns").fetchone()[0] == original_id
            return json.dumps([{"id": 1, "questions": ["What is the cat named?"]}]), {}

        report = anticipate.expand(store, complete)
        assert report["turns"] == report["indexed"] == 0
        assert anticipate.pending(store) == [original_id]
        assert (
            store.db.execute("SELECT COUNT(*) FROM units WHERE part>=?", (anticipate.EXPANSION_PART,)).fetchone()[0]
            == 0
        )


def test_anticipation_workers_obey_the_active_llm_budget(tmp_path, monkeypatch):
    from unimem import llm
    from unimem.llm_runtime import Budget, LLMRuntime

    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", "none")
    monkeypatch.setenv("UNIMEM_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("UNIMEM_LLM_CACHE", "0")
    calls = []

    def provider(*_args):
        calls.append(True)
        return json.dumps([{"id": 1, "questions": ["feline companion"]}]), {"input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(llm, "_provider_call", provider)
    with Store(str(tmp_path), "budget") as store:
        store.add("s", [{"speaker": "Ana", "role": "user", "text": f"Memory number {i}."} for i in range(3)])
        runtime = LLMRuntime(str(tmp_path), "anticipate", budget=Budget(calls=1))
        with runtime.scope():
            report = anticipate.expand(store, llm.complete, batch=1, workers=2)
        assert runtime.calls == 1 and calls == [True]
        assert report["indexed"] == 1 and report["failed_batches"] == 2


def test_prompt_rendering_is_bounded_without_changing_batch_results(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", "none")
    rendered, initial_backlog = [], []
    original = anticipate._render

    def render(store, batch):
        rendered.append(tuple(batch))
        return original(store, batch)

    monkeypatch.setattr(anticipate, "_render", render)
    with Store(str(tmp_path), "backlog") as store:
        store.add("s", [{"speaker": "Ana", "role": "user", "text": f"Memory number {i}."} for i in range(20)])

        def complete(prompt):
            if "Memory number 0." in prompt:
                initial_backlog.append(len(rendered))
            return json.dumps([{"id": 1, "questions": ["memory retrieval key"]}]), {}

        assert anticipate.expand(store, complete, batch=1, workers=2)["indexed"] == 20
        assert len(rendered) == 20 and 1 <= initial_backlog[0] <= 4


def test_write_failure_cancels_unstarted_model_work_and_preserves_error(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", "none")
    calls = []
    error = OSError("index write failed")

    def fail(*_args):
        raise error

    monkeypatch.setattr(anticipate, "_write", fail)
    with Store(str(tmp_path), "failure") as store:
        store.add("s", [{"speaker": "Ana", "role": "user", "text": f"Memory number {i}."} for i in range(20)])

        def complete(_):
            calls.append(True)
            return json.dumps([{"id": 1, "questions": ["memory retrieval key"]}]), {}

        with pytest.raises(OSError) as caught:
            anticipate.expand(store, complete, batch=1, workers=2)
        assert caught.value is error
        assert 1 <= len(calls) <= 4
        assert len(anticipate.pending(store)) == 20


def test_zero_limit_retains_pending_work_without_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", "none")
    with Store(str(tmp_path), "zero") as store:
        store.add("s", [{"speaker": "Ana", "role": "user", "text": "The cat is Miso."}])

        def complete(_):
            pytest.fail("zero-limit expansion must not dispatch")

        assert anticipate.expand(store, complete, limit=0) == {
            "turns": 0,
            "indexed": 0,
            "empty": 0,
            "rejected": 0,
            "failed_batches": 0,
        }
        assert len(anticipate.pending(store)) == 1
