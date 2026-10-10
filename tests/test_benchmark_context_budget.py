"""Measured caps preserve native source proofs and refuse unavoidable excess."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from benchmarks.context_budget import retrieve_with_cap


def test_oversized_context_is_reassembled_and_uses_final_native_source_proof():
    calls = []

    def retrieve(budget):
        calls.append(budget)
        return SimpleNamespace(context="x" * (budget + 20), turns=[budget], ranked=[1, budget])

    result, accounting = retrieve_with_cap(retrieve, 100, len)
    assert len(result.context) <= 100
    assert result.turns == [calls[-1]]
    assert result.ranked == [1, calls[-1]]
    assert accounting["native_budget"] == calls[-1]
    assert accounting["text_tokens"] == len(result.context)
    assert accounting["attempts"] == len(calls) > 1


def test_unavoidable_mandatory_context_refused_without_truncating_it():
    calls = []

    def retrieve(budget):
        calls.append(budget)
        return SimpleNamespace(context="mandatory directives" * 20, turns=[1])

    with pytest.raises(ValueError, match="within the attempt limit"):
        retrieve_with_cap(retrieve, 10, len)
    assert calls[-1] == 1


def test_nonmonotonic_native_assembly_still_checks_every_returned_context():
    sizes = iter([110, 150, 80])
    result, accounting = retrieve_with_cap(lambda b: SimpleNamespace(context="x" * next(sizes)), 100, len)
    assert len(result.context) == 80
    assert accounting["attempts"] == 3


@pytest.mark.parametrize("count", [lambda _: -1, lambda _: True, lambda _: 1.5])
def test_invalid_counter_is_refused(count):
    with pytest.raises(ValueError, match="invalid count"):
        retrieve_with_cap(lambda b: SimpleNamespace(context="hello"), 100, count)


def test_adversarial_assembly_has_a_finite_attempt_bound():
    calls = []
    with pytest.raises(ValueError, match="within the attempt limit"):
        retrieve_with_cap(lambda b: (calls.append(b) or SimpleNamespace(context="x" * 101)),
                          100, len, max_attempts=3)
    assert len(calls) == 3


def _fixture_counter(monkeypatch):
    from benchmarks import conversation_bench as bench

    monkeypatch.setattr(bench, "text_tokens", lambda text, tokenizer: len(text))
    monkeypatch.setattr(bench, "text_counter_binding", lambda name: {"fixture": name})
    return bench


def test_strict_requires_valid_counter_before_store_or_source_preparation(tmp_path, monkeypatch):
    from tests.test_benchmark_measurement import _args

    bench = _fixture_counter(monkeypatch)
    args = _args(tmp_path, strict_context_budget=True)
    with pytest.raises(ValueError, match="require --tokenizer"):
        bench.run(args)
    assert not (tmp_path / "store").exists()


def test_real_native_packing_and_budgeted_history_fit_selected_counter(tmp_path, monkeypatch):
    from tests.test_benchmark_measurement import _args

    bench = _fixture_counter(monkeypatch)
    args = _args(tmp_path, strict_context_budget=True, tokenizer="fixture", budget="100",
                 modes="memory,budgeted-history,full-context,no-memory")
    captured = []
    native = bench.recall

    def recall(*args, **kwargs):
        result = native(*args, **kwargs)
        captured.append(result)
        return result

    monkeypatch.setattr(bench, "recall", recall)
    result = bench.run(args)[100]
    for mode in ("memory", "budgeted-history"):
        rows = result["modes_detail"][mode]["rows"]
        assert all(row["context_text_tokens"] <= 100 for row in rows)
        assert all(row["context_budget"]["text_tokens"] == row["context_text_tokens"] for row in rows)
    memory_rows = result["rows"]
    assert any(row["context_budget"]["attempts"] > 1 for row in memory_rows)
    assert any(len(r.context) > 100 for r in captured)
    assert result["modes_detail"]["full-context"]["overall"]["context_text_tokens"] > 100
    assert result["modes_detail"]["no-memory"]["overall"]["context_text_tokens"] == 0
    assert result["provenance"]["evaluation"]["context_budget"]["full_context"] == "uncapped"


def test_counter_contract_changes_cannot_be_silently_paired(tmp_path, monkeypatch):
    from benchmarks.compare import compare_runs
    from tests.test_benchmark_measurement import _args

    bench = _fixture_counter(monkeypatch)
    args = _args(tmp_path, tokenizer="fixture", budget="100")
    estimated = bench.run(args)[100]
    args.strict_context_budget = True
    strict = bench.run(args)[100]
    with pytest.raises(ValueError, match="provenance"):
        compare_runs(estimated, strict)


def test_unfitting_native_context_fails_before_reader_dispatch(tmp_path, monkeypatch):
    from commontrace import llm
    from commontrace.conversation.search import Recall
    from tests.test_benchmark_measurement import _args

    bench = _fixture_counter(monkeypatch)
    args = _args(tmp_path, strict_context_budget=True, tokenizer="fixture", budget="10", answer=True,
                 no_cache=True)
    monkeypatch.setattr(bench, "get_judge", lambda *a, **kw: SimpleNamespace(profile="fixture"))
    config = llm.Config(provider="openai-compatible", model="fixture", api_key="unused",
                        base_url="http://127.0.0.1:1/v1")
    monkeypatch.setattr(bench, "_get_llm_config", lambda model: config)
    monkeypatch.setattr(bench, "recall", lambda *a, **kw: Recall("", "mandatory" * 20, 45, [], [], None))
    calls = []
    monkeypatch.setattr(bench, "grade_answer", lambda **kw: calls.append(kw))
    with pytest.raises(ValueError, match="within the attempt limit"):
        bench.run(args)
    assert not calls


def test_counter_drift_invalidates_completed_measurement(tmp_path, monkeypatch):
    from tests.test_benchmark_measurement import _args

    bench = _fixture_counter(monkeypatch)
    bindings = iter([{"fixture": "original"}, {"fixture": "changed"}])
    monkeypatch.setattr(bench, "text_counter_binding", lambda name: next(bindings))
    args = _args(tmp_path, strict_context_budget=True, tokenizer="fixture", budget="100")
    with pytest.raises(RuntimeError, match="counter changed"):
        bench.run(args)


def test_reference_only_modes_skip_unrequested_memory_retrieval(tmp_path, monkeypatch):
    from tests.test_benchmark_measurement import _args

    bench = _fixture_counter(monkeypatch)
    args = _args(tmp_path, strict_context_budget=True, tokenizer="fixture", budget="10",
                 modes="full-context,no-memory,budgeted-history")

    def forbidden(*args, **kwargs):
        raise AssertionError("unrequested memory retrieval")

    monkeypatch.setattr(bench, "recall", forbidden)
    details = bench.run(args)[10]["modes_detail"]
    assert details["full-context"]["overall"]["context_text_tokens"] > 10
    assert details["no-memory"]["overall"]["context_text_tokens"] == 0
    assert details["budgeted-history"]["overall"]["context_text_tokens"] <= 10


def test_native_vendor_is_reassembled_instead_of_context_truncation(tmp_path, monkeypatch):
    import contextlib

    from commontrace.conversation.search import Recall
    from tests.test_benchmark_measurement import _args

    bench = _fixture_counter(monkeypatch)
    calls = []

    @contextlib.contextmanager
    def profile(name, store, now):
        def retrieve(question, budget):
            calls.append(budget)
            return Recall(question, "x" * (budget + 20), budget, [1] if budget == 100 else [2],
                          [1, 2], None)
        yield SimpleNamespace(descriptor={"profile": "native"}, retrieve=retrieve)

    monkeypatch.setattr(bench, "vendor_profile", profile)
    args = _args(tmp_path, strict_context_budget=True, tokenizer="fixture", budget="100",
                 memory_adapter="mem0-raw")
    rows = bench.run(args)[100]["rows"]
    assert all(row["context_budget"]["attempts"] > 1 for row in rows)
    # The final native selection was turn 2; the discarded first pass selected gold turn 1.
    assert all(row["evidence"] == 0 for row in rows)
    assert all(row["context_text_tokens"] <= 100 for row in rows)
    assert len(calls) > len(rows)


def test_tokenizer_binding_detects_vocabulary_or_regex_changes(monkeypatch):
    from benchmarks import requests

    encoding = SimpleNamespace(_pat_str="pattern", _special_tokens={"end": 2},
                               _mergeable_ranks={b"a": 0, b"b": 1})
    monkeypatch.setattr(requests, "_encoding", lambda name: encoding)
    # Avoid making tiktoken a requirement of the unit regression.
    import importlib.metadata
    monkeypatch.setattr(importlib.metadata, "version", lambda package: "fixture")
    before = requests.text_counter_binding("fixture")
    encoding._mergeable_ranks[b"b"] = 3
    assert requests.text_counter_binding("fixture") != before
    before = requests.text_counter_binding("fixture")
    encoding._pat_str = "new pattern"
    assert requests.text_counter_binding("fixture") != before
