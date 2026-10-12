"""Retrieval telemetry reports raw score scales without leaking query contents."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from commontrace import fact_store, hierarchical, recall, telemetry


class _RecordingSpan:
    def __init__(self, name: str, output: list[tuple[str, dict]]) -> None:
        self.name = name
        self.attributes: dict[str, object] = {}
        self._output = output
        output.append((name, self.attributes))

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


class _RecordingTracer:
    def __init__(self) -> None:
        self.spans: list[tuple[str, dict]] = []

    @contextmanager
    def start_as_current_span(self, name: str):
        yield _RecordingSpan(name, self.spans)


def _install_tracer(monkeypatch: pytest.MonkeyPatch) -> _RecordingTracer:
    tracer = _RecordingTracer()
    monkeypatch.setattr(telemetry, "_tracer", lambda: tracer)
    return tracer


def _recall_span(tracer: _RecordingTracer) -> dict[str, object]:
    return next(attrs for name, attrs in tracer.spans if name == "recall.multi")


def test_recall_span_reports_raw_fused_score_and_heuristic_assessment(tmp_path, monkeypatch):
    root = str(tmp_path)
    fact_store.migrate(root)
    fact, _ = hierarchical.add_fact(root, "The parser validates JSON before execution.")
    hierarchical.add_fact(root, "The parser validates YAML before saving operations.")
    query = "how does the parser validate JSON before execution"
    monkeypatch.setenv("COMMONTRACE_FACT_EMBEDDER", "none")
    tracer = _install_tracer(monkeypatch)

    result = recall.recall(root, query, channels=("facts",), reranker="none")

    fields = _recall_span(tracer)
    assert len(result.items) >= 2
    scores = sorted((item.fused for item in result.items), reverse=True)
    assert fields["commontrace.recall_top_score_raw"] == max(item.fused for item in result.items)
    assert fields["commontrace.recall_score_scale"] == "weighted_rrf"
    assert fields["commontrace.recall_score_margin_raw"] == pytest.approx(scores[0] - scores[1])
    assert fields["commontrace.recall_assessment_confidence"] == result.assessment.confidence
    assert fields["commontrace.recall_abstained"] is result.assessment.abstain
    assert not any(key.endswith("recall_hit") for key in fields)
    wire = repr(fields)
    assert query not in wire and fact.statement not in wire and fact.id not in wire


def test_empty_recall_reports_zero_raw_score_without_claiming_a_hit(tmp_path, monkeypatch):
    root = str(tmp_path)
    fact_store.migrate(root)
    monkeypatch.setenv("COMMONTRACE_FACT_EMBEDDER", "none")
    tracer = _install_tracer(monkeypatch)

    result = recall.recall(root, "unrelated zebra question", channels=("facts",), reranker="none")

    fields = _recall_span(tracer)
    assert not result.items
    assert fields["commontrace.recall_top_score_raw"] == 0.0
    assert fields["commontrace.recall_score_scale"] == "weighted_rrf"
    assert fields["commontrace.recall_score_margin_raw"] == 0.0
    assert fields["commontrace.recall_abstained"] is True
    assert not any(key.endswith("recall_hit") for key in fields)
