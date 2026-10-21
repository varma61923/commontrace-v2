"""Pinned GenAI attribute names and admission before observable spans."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from unimem import hierarchical, otel_memory, search_recipes, telemetry


class Tracer:
    def __init__(self):
        self.spans = []

    @contextmanager
    def start_as_current_span(self, name, **options):
        attributes = {}
        class Span:
            def set_attribute(self, key, value):
                attributes[key] = value
        self.spans.append({"name": name, "attributes": attributes, **options})
        yield Span()


def test_pinned_operations_and_exact_standard_attribute_keys(tmp_path):
    root = str(tmp_path)
    memory, _ = hierarchical.add_fact(root, "Verify production deployment signatures.")
    assert otel_memory.CONVENTION_VERSION == "f3c6d0c9bb66ebd474668a3a0be25c61226f02c2"
    for action, operation in otel_memory.OPERATIONS.items():
        attributes = otel_memory.attributes(root, action, [memory.id])
        assert attributes["gen_ai.operation.name"] == operation
        assert attributes["gen_ai.memory.record.id"] == memory.id
        assert attributes["gen_ai.memory.record.count"] == 1
        assert "gen_ai.memory.store.id" in attributes
        assert all(k.startswith(("gen_ai.", "unimem.")) for k in attributes)
        assert all("contents" not in k and "query.text" not in k for k in attributes)


def test_invisible_memory_never_produces_a_span_or_count(tmp_path, monkeypatch):
    root = str(tmp_path)
    private, _ = hierarchical.add_fact(root, "Restricted release evidence.", scopes=["secret"])
    tracer = Tracer()
    monkeypatch.setattr(telemetry, "_tracer", lambda: tracer)
    otel_memory.configure(root, enabled=True, include_content=True)
    with otel_memory.operation(root, "search", [private.id], query="private subject") as span:
        assert span == {"span_created": False, "attributes": {}}
    assert tracer.spans == []
    with otel_memory.operation(root, "search", [private.id], context=["secret"], query="authorized question"):
        pass
    assert len(tracer.spans) == 1
    assert tracer.spans[0]["attributes"]["gen_ai.memory.query.text"] == "authorized question"
    assert not any(k.startswith("unimem.gen_ai") for k in tracer.spans[0]["attributes"])


def test_missing_sdk_degrades_cleanly_and_causal_extensions_are_bounded(tmp_path, monkeypatch):
    root = str(tmp_path)
    memory, _ = hierarchical.add_fact(root, "Check backups before deploying.")
    otel_memory.configure(root, enabled=True)
    monkeypatch.setattr(telemetry, "_tracer", lambda: None)
    with otel_memory.operation(root, "search", [memory.id], extensions={
            "holdout.arm": "treated", "holdout.propensity": .5, "delegation.chain_digest": "digest",
            "isolation.level": "serializable", "n_eff": 2}) as span:
        assert not span["span_created"] and span["attributes"]["unimem.holdout.propensity"] == .5
    for extensions in ({"unknown": "value"}, {"n_eff": float("nan")}, {"n_eff": {}}):
        with pytest.raises(ValueError):
            otel_memory.attributes(root, "search", [memory.id], extensions=extensions)


def test_configured_fact_and_search_paths_emit_admitted_spans(tmp_path, monkeypatch):
    root = str(tmp_path)
    tracer = Tracer()
    monkeypatch.setattr(telemetry, "_tracer", lambda: tracer)
    otel_memory.configure(root, enabled=True)
    memory, _ = hierarchical.add_fact(root, "Deployments require verified production signatures.")
    search_recipes.search(root, "production signatures", reranker="none")
    assert [s["name"] for s in tracer.spans] == ["upsert_memory", "search_memory"]
    assert all(s["start_time"] is not None for s in tracer.spans)
    assert all(s["attributes"]["gen_ai.memory.record.id"] == memory.id for s in tracer.spans)


def test_pinned_fixture_batch_types_and_authenticated_writer(tmp_path):
    import json
    from pathlib import Path

    from unimem import memory_authority

    fixture = json.loads((Path(__file__).parent / "fixtures" / "otel_memory_contract.json").read_text())
    root = str(tmp_path)
    with memory_authority.writer("actual-writer", "agent"):
        memories = hierarchical.append_facts(root, [{"statement": "Inspect deployment signatures."},
                                                   {"statement": "Measure backup restoration outcomes."}])
    ids = [f.id for f, _ in memories]
    attributes = otel_memory.attributes(root, "search", ids)
    assert attributes["gen_ai.operation.name"] in fixture["operations"]
    assert fixture["specific_single_record_attribute"] not in attributes
    assert attributes["gen_ai.memory.record.count"] == 2
    assert attributes["unimem.writer.agent"] == "actual-writer"
    assert not set(fixture["opt_in_attributes"]) & attributes.keys()
    for key, value in attributes.items():
        if key in fixture["attributes"]:
            assert type(value) is {"string": str, "integer": int}[fixture["attributes"][key]]


def test_telemetry_failure_never_turns_a_committed_write_into_a_failure(tmp_path, monkeypatch):
    root = str(tmp_path)
    class BrokenTracer:
        def start_as_current_span(self, *args, **kwargs):
            raise RuntimeError("exporter unavailable")
    monkeypatch.setattr(telemetry, "_tracer", lambda: BrokenTracer())
    otel_memory.configure(root, enabled=True)
    memory, _ = hierarchical.add_fact(root, "Check deploy rollback evidence.")
    assert hierarchical.load_facts(root)[memory.id].statement == memory.statement
    assert any(row["name"] == "unimem_telemetry_failures" for row in telemetry.metrics()["counters"])


def test_error_span_emits_only_exception_class_and_opt_in_is_scope_filtered(tmp_path, monkeypatch):
    root = str(tmp_path)
    visible, _ = hierarchical.add_fact(root, "Confirm backup restoration.")
    hidden, _ = hierarchical.add_fact(root, "Private operator information.", scopes=["secret"])
    tracer = Tracer()
    monkeypatch.setattr(telemetry, "_tracer", lambda: tracer)
    otel_memory.configure(root, enabled=True, include_content=True)
    with pytest.raises(ValueError):
        with otel_memory.operation(root, "update", [visible.id]):
            raise ValueError("private exception message must not leave")
    assert tracer.spans[0]["attributes"]["error.type"] == "ValueError"
    assert "private exception message" not in str(tracer.spans)
    assert tracer.spans[0]["record_exception"] is False
    mixed = otel_memory.attributes(root, "search", [visible.id, hidden.id], include_content=True, query="private")
    assert "gen_ai.memory.query.text" not in mixed and "gen_ai.memory.records" not in mixed
    assert mixed["gen_ai.memory.record.count"] == 1


def test_disabled_wrapper_uses_only_its_cheap_flag(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(otel_memory.os.path, "isfile", lambda path: calls.append(path) or False)
    monkeypatch.setattr(otel_memory, "_path", lambda root: pytest.fail("disabled telemetry resolved a filesystem boundary"))
    @otel_memory.observed("search")
    def operation(root):
        return []
    assert operation(str(tmp_path)) == [] and len(calls) == 1


def test_create_update_delete_use_real_operations_without_resurrecting_data(tmp_path, monkeypatch):
    from unimem import privacy_erasure

    root = str(tmp_path)
    tracer = Tracer()
    monkeypatch.setattr(telemetry, "_tracer", lambda: tracer)
    otel_memory.configure(root, enabled=True)
    memory = hierarchical.append_facts(root, [{"statement": "Monitor wrist current during loading."}])[0][0]
    hierarchical.update_fact(root, memory.id, confidence=.9)
    privacy_erasure.purge_fact(root, memory.id, reason="operator deletion")
    assert [s["name"] for s in tracer.spans] == ["create_memory", "update_memory", "delete_memory"]
    assert memory.id not in hierarchical.load_facts(root)


def test_outcome_spans_use_canonical_arms_and_omit_invisible_memories(tmp_path, monkeypatch):
    from unimem import holdout_io

    root = str(tmp_path)
    visible, _ = hierarchical.add_fact(root, "Check controlled release signatures.")
    hidden, _ = hierarchical.add_fact(root, "Private task evidence.", scopes=["secret"])
    holdout_io.configure(root, rate=.5, salt="otel-arms")
    withheld = holdout_io.assign_and_log(root, [visible.id, hidden.id], occasion_id="task-1", rate=.5,
                                      salt="otel-arms", revisions={visible.id: visible.revision, hidden.id: hidden.revision})
    holdout_io.record_outcome(root, "task-1", True)
    tracer = Tracer()
    monkeypatch.setattr(telemetry, "_tracer", lambda: tracer)
    otel_memory.configure(root, enabled=True)
    otel_memory.outcome(root, "task-1")
    assert len(tracer.spans) == 1
    span = tracer.spans[0]
    assert span["name"] == "unimem.task.outcome"
    assert span["attributes"]["gen_ai.memory.record.id"] == visible.id
    assert span["attributes"]["unimem.holdout.arm"] == ("control" if visible.id in withheld else "treated")
    assert span["attributes"]["unimem.outcome.succeeded"] is True
    assert span["attributes"]["unimem.holdout.propensity"] == .5
