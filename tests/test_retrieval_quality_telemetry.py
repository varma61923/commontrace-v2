"""Retrieval telemetry reports raw score scales without leaking query contents."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from unimem import fact_store, hierarchical, recall, telemetry


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
    monkeypatch.setenv("UNIMEM_FACT_EMBEDDER", "none")
    tracer = _install_tracer(monkeypatch)

    result = recall.recall(root, query, channels=("facts",), reranker="none")

    fields = _recall_span(tracer)
    assert len(result.items) >= 2
    scores = sorted((item.fused for item in result.items), reverse=True)
    assert fields["unimem.recall_top_score_raw"] == max(item.fused for item in result.items)
    assert fields["unimem.recall_score_scale"] == "weighted_rrf"
    assert fields["unimem.recall_score_margin_raw"] == pytest.approx(scores[0] - scores[1])
    assert fields["unimem.recall_assessment_confidence"] == result.assessment.confidence
    assert fields["unimem.recall_abstained"] is result.assessment.abstain
    assert not any(key.endswith("recall_hit") for key in fields)
    wire = repr(fields)
    assert query not in wire and fact.statement not in wire and fact.id not in wire


def test_empty_recall_reports_zero_raw_score_without_claiming_a_hit(tmp_path, monkeypatch):
    root = str(tmp_path)
    fact_store.migrate(root)
    monkeypatch.setenv("UNIMEM_FACT_EMBEDDER", "none")
    tracer = _install_tracer(monkeypatch)

    result = recall.recall(root, "unrelated zebra question", channels=("facts",), reranker="none")

    fields = _recall_span(tracer)
    assert not result.items
    assert fields["unimem.recall_top_score_raw"] == 0.0
    assert fields["unimem.recall_score_scale"] == "weighted_rrf"
    assert fields["unimem.recall_score_margin_raw"] == 0.0
    assert fields["unimem.recall_abstained"] is True
    assert not any(key.endswith("recall_hit") for key in fields)


def _conversation_options(**changes):
    from unimem.conversation import Options

    return Options(**{
        "embedder": None, "rerank": None, "entity_boost": 0.0, "recency_boost": 0.0,
        "graph_hops": 0, "profile_facts": 0, "instructions": 0, "summaries": False,
        "neighbours_before": 0, "neighbours_after": 0, **changes,
    })


def _conversation_spans(tracer):
    return [attrs for name, attrs in tracer.spans if name == "conversation.recall"]


def test_direct_conversation_quality_is_identical_on_fresh_and_cached_paths(tmp_path, monkeypatch):
    from unimem.conversation import Store
    from unimem.conversation import recall as conversation_recall

    tracer = _install_tracer(monkeypatch)
    query = "parser validates JSON"
    private_space = "private-user-space-uncaptured"
    with Store(str(tmp_path), private_space) as store:
        store.add("private-session-uncaptured", [
            {"speaker": "operator", "text": "The parser validates JSON before execution in the private vault."},
            {"speaker": "operator", "text": "The parser validates JSON before saving state."},
        ])
        fresh = conversation_recall(store, query, options=_conversation_options())
        assert len(fresh.turns) == 2
        # Public callers cannot poison the retained summary on the cache hit.
        fresh.explain["retrieval_score"]["top"] = "private-secret-uncaptured"
        cached = conversation_recall(store, query, options=_conversation_options())
        fields = _conversation_spans(tracer)
        assert len(fields) == 2
        for values in fields:
            assert values["unimem.recall_top_score_raw"] == pytest.approx(1 / 61)
            assert values["unimem.recall_score_margin_raw"] == pytest.approx(1 / 61 - 1 / 62)
            assert values["unimem.recall_score_scale"] == "conversation_weighted_rrf"
            assert values["unimem.recall_score_stage"] == "first_stage"
            assert values["unimem.recall_assessment_confidence"] == cached.explain["confidence"]
            assert values["unimem.recall_abstained"] is False
            assert not any(key.endswith("recall_hit") for key in values)
            wire = repr(values)
            for private in (query, private_space, "private-session-uncaptured", "private-secret-uncaptured",
                            "The parser validates JSON before execution in the private vault."):
                assert private not in wire
        assert fields[1]["unimem.cached"] is True
        assert cached.explain["retrieval_score"]["top"] == pytest.approx(1 / 61)


@pytest.mark.parametrize("query", ["", "unrelated zebra question"])
def test_direct_empty_conversation_quality_is_zero_and_abstained(tmp_path, monkeypatch, query):
    from unimem.conversation import Store
    from unimem.conversation import recall as conversation_recall

    tracer = _install_tracer(monkeypatch)
    with Store(str(tmp_path), "empty") as store:
        for _ in range(2):
            result = conversation_recall(store, query, options=_conversation_options())
            assert not result.context and not result.turns
    for fields in _conversation_spans(tracer):
        assert fields["unimem.recall_top_score_raw"] == 0.0
        assert fields["unimem.recall_score_margin_raw"] == 0.0
        assert fields["unimem.recall_score_scale"] == "conversation_weighted_rrf"
        assert fields["unimem.recall_assessment_confidence"] == 0.0
        assert fields["unimem.recall_abstained"] is True
        assert not any(key.endswith("recall_hit") for key in fields)


def test_direct_conversation_reports_delivered_evidence_not_unpacked_candidates(tmp_path, monkeypatch):
    from unimem.conversation import Store
    from unimem.conversation import recall as conversation_recall

    tracer = _install_tracer(monkeypatch)
    with Store(str(tmp_path), "tiny") as store:
        store.add("s", [{"speaker": "operator", "text": "The parser validates JSON before execution."}])
        result = conversation_recall(store, "parser validates JSON", options=_conversation_options(budget=0))
        assert result.ranked and not result.turns
    fields = _conversation_spans(tracer)[0]
    assert fields["unimem.recall_top_score_raw"] == 0.0
    assert fields["unimem.recall_score_margin_raw"] == 0.0
    assert fields["unimem.recall_abstained"] is True


def test_direct_conversation_quality_includes_delivered_second_pass_scores(tmp_path, monkeypatch):
    from unimem.conversation import Store
    from unimem.conversation import recall as conversation_recall

    tracer = _install_tracer(monkeypatch)
    query = "Which tomatoes and peppers are on the trellis?"
    with Store(str(tmp_path), "garden") as store:
        for index in range(2):
            store.add(f"tomatoes-{index}", [{
                "speaker": "Ana", "text": f"Trellis tomatoes, trellis tomatoes: trellis {index} of tomatoes.",
            }], session_at=f"2024-05-{index + 1:02d}")
        for index in range(4):
            store.add(f"peppers-{index}", [{
                "speaker": "Ana", "text": f"Long chatter {index} about the weekend, the weather, the traffic, "
                "the neighbours, the news and also some peppers.",
            }], session_at=f"2024-06-{index + 10:02d}")
        first = conversation_recall(store, query, options=_conversation_options(pool=2, budget=400))
        second = conversation_recall(store, query, options=_conversation_options(pool=2, budget=400, second_pass=True))
        cached = conversation_recall(store, query, options=_conversation_options(pool=2, budget=400, second_pass=True))
        assert second.explain["second_pass"]["added_turns"]
        assert set(second.explain["second_pass"]["added_turns"]) <= set(second.turns) - set(first.turns)
        assert second.explain["coverage"]["missing_subject_terms"] == []
        assert cached.turns == second.turns
    fields = _conversation_spans(tracer)
    assert fields[0]["unimem.recall_score_margin_raw"] == pytest.approx(1 / 61 - 1 / 62)
    # The newly delivered facet's first-ranked turn ties the original top turn.
    # Omitting second-pass scores would incorrectly retain the positive margin.
    for values in fields[1:]:
        assert values["unimem.recall_top_score_raw"] == pytest.approx(1 / 61)
        assert values["unimem.recall_score_margin_raw"] == 0.0
        assert values["unimem.recall_abstained"] is False


def test_direct_profile_only_context_has_no_invented_ranking_score(tmp_path, monkeypatch):
    from unimem.conversation import Store
    from unimem.conversation import recall as conversation_recall

    tracer = _install_tracer(monkeypatch)
    with Store(str(tmp_path), "profile") as store:
        store.add("s", [{"speaker": "Ana", "text": "I live in London."}], session_at="2025-01-01")
        for _ in range(2):
            result = conversation_recall(store, "Where does Ana live?",
                                         options=_conversation_options(pool=0, profile_facts=4))
            assert "London" in result.context and not result.ranked
            assert result.explain["confidence"] == 1.0
    for fields in _conversation_spans(tracer):
        assert fields["unimem.recall_top_score_raw"] == 0.0
        assert fields["unimem.recall_score_margin_raw"] == 0.0
        assert fields["unimem.recall_assessment_confidence"] == 1.0
        assert fields["unimem.recall_abstained"] is False


def test_direct_conversation_does_not_enable_export(tmp_path, monkeypatch):
    from unimem.conversation import Store
    from unimem.conversation import recall as conversation_recall

    monkeypatch.delenv("UNIMEM_OTEL", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(telemetry, "_TRACER", None)
    monkeypatch.setitem(telemetry._TRACER_STATE, "tried", False)
    with Store(str(tmp_path), "local") as store:
        conversation_recall(store, "zebra", options=_conversation_options())
    assert not telemetry.otel_enabled()
    assert telemetry._TRACER is None
