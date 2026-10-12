"""Deterministic, offline evaluation for graph resolution and extraction heuristics."""
from benchmarks import graph_quality_eval as quality


def test_binary_pair_metrics_are_exact_and_include_false_negatives():
    assert quality.binary_metrics([True, True, False], [True, False, True]) == {
        "examples": 3,
        "positive_labels": 2,
        "predicted_positive": 2,
        "tp": 1,
        "fp": 1,
        "fn": 1,
        "tn": 0,
        "precision": 0.5,
        "recall": 0.5,
        "f1": 0.5,
    }


def test_fixture_eval_is_deterministic_and_marks_model_tiers_unmeasured(monkeypatch):
    monkeypatch.delenv("COMMONTRACE_GRAPH_EMBEDDER", raising=False)
    monkeypatch.delenv("COMMONTRACE_LLM_PROVIDER", raising=False)
    first = quality.evaluate()
    second = quality.evaluate()

    assert first == second
    assert first["schema_version"] == "1.0.0"
    legacy = first["legacy_ngram_resolution"]
    assert legacy["thresholds"] == {"report": 0.6, "apply": 0.85}
    assert legacy["report_threshold_metrics"]["tp"] == 11
    assert legacy["report_threshold_metrics"]["fp"] == 4
    assert legacy["report_threshold_metrics"]["fn"] == 2
    assert legacy["apply_threshold_metrics"]["tp"] == 8
    assert legacy["apply_threshold_metrics"]["fp"] == 1
    assert legacy["apply_threshold_metrics"]["fn"] == 5
    assert first["entity_resolution"]["scope"] == "exact+MinHash/entropy only"
    assert first["entity_resolution"]["embedding"] == "unmeasured"
    assert first["entity_resolution"]["llm"] == "unmeasured"
    assert first["classification"]["methods"] == ["keyword"]
    assert first["relation_extraction"]["method"] == "pattern"
    assert first["threshold_decisions"]["entity_embedding_cosine_and_top_k"] == (
        "preserved; no labeled model scores or judged top-k candidates"
    )


def test_fixture_has_positive_and_negative_resolution_and_extraction_labels():
    data = quality.load_fixture(quality.DEFAULT_FIXTURE)

    assert any(row["duplicate"] for row in data["entity_pairs"])
    assert any(not row["duplicate"] for row in data["entity_pairs"])
    assert any(row["expected"] is None for row in data["classifications"])
    assert any(row["triples"] for row in data["relation_extraction"])
    assert any(not row["triples"] for row in data["relation_extraction"])
