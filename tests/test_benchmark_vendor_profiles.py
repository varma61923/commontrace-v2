"""Source, clock and budget boundaries of optional vendor evaluation."""
from __future__ import annotations

import contextlib
import copy
import os

import pytest

from benchmarks import conversation_bench as bench
from benchmarks.vendor_adapters import eligible_turns, pack, vendor_profile
from commontrace.conversation import Store
from tests.test_benchmark_measurement import _args


def test_locomo_question_limit_exact_stratified_and_repeatable():
    cases = [(f"c{i}", [], None, [
        {"id": f"{i}-{kind}-{j}", "type": kind}
        for kind in ("single-hop", "temporal") for j in range(8)]) for i in range(3)]
    before = copy.deepcopy(cases)
    selected = bench.sample_cases(cases, 11, 19)
    identities = [q["id"] for _s, _ss, _n, qs in selected for q in qs]
    assert len(identities) == len(set(identities)) == 11
    assert len(selected) == 3
    assert {q["type"] for _s, _ss, _n, qs in selected for q in qs} == {"single-hop", "temporal"}
    assert selected == bench.sample_cases(cases, 11, 19)
    assert selected != bench.sample_cases(cases, 11, 20)
    assert cases == before
    assert bench.sample_cases(cases, 1000, 19) == cases


def test_vendor_cutoff_precedes_indexing_and_invalid_clock_refused(tmp_path):
    with Store(str(tmp_path), "test") as store:
        store.add("old", [{"id": "old", "text": "Mango the cat."}], session_at="2025-01-01")
        store.add("later", [{"id": "future", "text": "Dragon the pet."}], session_at="2027-01-01")
        assert [t.ref for t in eligible_turns(store, "2026-01-01")] == ["old"]
        with pytest.raises(ValueError, match="parseable"):
            eligible_turns(store, "bad date")


def test_vendor_packing_has_source_attribution_and_no_oversized_partial_proof(tmp_path):
    with Store(str(tmp_path), "test") as store:
        store.add("s", [{"id": "big", "text": "long " * 100}, {"id": "short", "text": "Mango"}],
                  session_at="2025-01-01")
        result = pack(store, [1, 1, 2], 40, profile="test")
        assert result.ranked == [1, 2]
        assert result.turns == [2]
        assert result.tokens <= 40
        assert "short | 2025" in result.context
        assert "long " not in result.context
        with pytest.raises(ValueError, match="outside"):
            pack(store, [100], 100, profile="test")


def test_vendor_runs_native_adapter_not_commontrace_recall(tmp_path, monkeypatch):
    args = _args(tmp_path, memory_adapter="mem0-raw", limit=2)
    calls, closed = [], []

    class Adapter:
        descriptor = {"profile": "test-native"}

        def retrieve(self, question, budget):
            calls.append((question, budget))
            return pack(self.store, [1], budget, profile="test-native")

    @contextlib.contextmanager
    def profile(name, store, now):
        assert name == "mem0-raw"
        adapter = Adapter()
        adapter.store = store
        try:
            yield adapter
        finally:
            closed.append(store.space)

    def forbidden(*args, **kwargs):
        raise AssertionError("CommonTrace recall must not stand in for vendor retrieval")

    monkeypatch.setattr(bench, "vendor_profile", profile)
    monkeypatch.setattr(bench, "recall", forbidden)
    result = bench.run(args)
    assert len(calls) == 4
    assert len(closed) == 2
    assert result[1500]["overall"]["n"] == 2
    assert result[1500]["retrieval_profile"] == {"profile": "test-native"}
    assert all(row["memory_adapter"] == "mem0-raw" for row in result[1500]["rows"])


def test_unknown_profile_refused(tmp_path):
    with Store(str(tmp_path), "test") as store:
        with pytest.raises(ValueError, match="unsupported"):
            with vendor_profile("made-up", store, None):
                pass


@pytest.mark.skipif(os.environ.get("COMMONTRACE_VENDOR_NATIVE_SMOKE") != "1",
                    reason="opt-in native local Graphiti backend")
@pytest.mark.parametrize("space", ["demo", "conv-26", "gpt4_93159ced"])
def test_native_graphiti_case_namespaces_preserve_sources(tmp_path, space):
    pytest.importorskip("graphiti_core")
    pytest.importorskip("redislite.async_falkordb_client")
    with Store(str(tmp_path), space) as store:
        store.add("s", [{"id": "evidence", "text": "Caroline visited the LGBTQ support group on May 7."}],
                  session_at="2025-01-01")
        store.add("future", [{"id": "future", "text": "Caroline visited the LGBTQ group tomorrow."}],
                  session_at="2027-01-01")
        with vendor_profile("graphiti-episodic", store, "2026-01-01") as adapter:
            result = adapter.retrieve("When did Caroline go to the LGBTQ support group?", 1000)
            assert result.turns == [1]
            assert result.ranked == [1]
            assert "May 7" in result.context
            assert "tomorrow" not in result.context
            assert adapter.descriptor["group_namespace"] == "sha256-canonical-space-utf8"


def test_small_limit_rotates_types_and_conversations():
    cases = [(f"c{i}", [], None, [{"id": f"{i}-{kind}", "type": kind}
                                 for kind in ("a", "b", "c", "d")]) for i in range(10)]
    selected = bench.sample_cases(cases, 10, 17)
    assert len(selected) == 10
    types = [q["type"] for _s, _ss, _n, qs in selected for q in qs]
    assert set(types) == {"a", "b", "c", "d"}
    assert max(types.count(kind) for kind in set(types)) <= 3


@pytest.mark.parametrize("failure", ["ingestion", "encoder"])
def test_mem0_failed_setup_releases_all_returned_resources(tmp_path, monkeypatch, failure):
    import sys
    import types

    from benchmarks import vendor_adapters as vendors

    closed = []
    embedding = types.SimpleNamespace(
        embed=lambda *args: [0.], embed_batch=lambda texts, action: [[0.] for _ in texts])

    class Memory:
        embedding_model = embedding
        llm = types.SimpleNamespace(client=types.SimpleNamespace(close=lambda: closed.append("llm")))
        vector_store = types.SimpleNamespace(
            client=types.SimpleNamespace(close=lambda: closed.append("qdrant")))

        @classmethod
        def from_config(cls, config):
            instance = cls()
            instance.vector_store._get_bm25_encoder = lambda: (_ for _ in ()).throw(RuntimeError("encoder"))
            return instance

        def add(self, *args, **kwargs):
            if failure == "ingestion":
                raise RuntimeError("ingestion")
            return {"results": [{"id": "raw", "event": "ADD"}]}

        def close(self):
            closed.append("history")

    package = types.ModuleType("mem0")
    package.Memory = Memory
    subpackage = types.ModuleType("mem0.memory")
    subpackage.telemetry = types.SimpleNamespace(MEM0_TELEMETRY=True)
    monkeypatch.setitem(sys.modules, "mem0", package)
    monkeypatch.setitem(sys.modules, "mem0.memory", subpackage)
    monkeypatch.setattr(vendors, "package_source", lambda package: "source")
    monkeypatch.setattr(vendors, "model_artifact", lambda name: {"model": name})
    monkeypatch.setattr(vendors.importlib.metadata, "version", lambda package: "test")
    with Store(str(tmp_path), "test") as store:
        store.add("s", [{"text": "Mango the cat."}])
        with pytest.raises(RuntimeError, match=failure):
            vendors.Mem0Raw(str(tmp_path), store, None)
    assert closed == ["history", "qdrant", "llm"]


def test_model_binding_includes_nested_pooling_configuration(tmp_path, monkeypatch):
    import sys
    import types

    from benchmarks.artifacts import model_artifact

    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "config.json").write_text("{}")
    (snapshot / "model.safetensors").write_bytes(b"weights")
    pooling = snapshot / "1_Pooling"
    pooling.mkdir()
    config = pooling / "config.json"
    config.write_text('{"mean":true}')
    hub = types.ModuleType("huggingface_hub")
    hub.try_to_load_from_cache = lambda *args: str(snapshot / "config.json")
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    before = model_artifact("fixture")
    config.write_text('{"mean":false}')
    assert model_artifact("fixture") != before
    assert "1_Pooling/config.json" in before["artifacts_sha256"]


def test_exact_context_text_counts_report_without_answering(tmp_path, monkeypatch):
    args = _args(tmp_path, tokenizer="tiktoken:fixture", budget="100")
    monkeypatch.setattr(bench, "text_tokens", lambda context, tokenizer: 101 if context else 0)
    summary = bench.run(args)[100]
    assert summary["overall"]["accuracy"] is None
    assert summary["overall"]["context_text_tokens"] == 101
    assert summary["overall"]["exact_budget_exceedances"] == 3
    assert summary["provenance"]["evaluation"]["context_text_tokenizer"] == "tiktoken:fixture"


def test_unavailable_requested_reranker_is_not_reported_as_effective(tmp_path, monkeypatch):
    from commontrace import rerank_arm

    args = _args(tmp_path, rerank="cross-encoder")
    monkeypatch.setattr(bench, "model_artifact", lambda name: {"fixture": name})
    monkeypatch.setattr(rerank_arm, "ready", lambda mode: "model unavailable")
    summary = bench.run(args)[1500]
    assert all(row["effective_rerank"] is None for row in summary["rows"])
