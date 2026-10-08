import json
from dataclasses import replace

import pytest

from benchmarks.conversation_retrieval import Measurement, clustered_delta, evaluate, load_cases, source_retention
from commontrace.conversation import Store


def _dataset(path, count=5):
    payload = [{"sample_id": "conversation", "conversation": {
        "session_1_date_time": "2025-01-01", "session_1": [
            {"dia_id": "D1:1", "speaker": "Nia", "text": "The garden grows lavender."},
            {"dia_id": "D1:2", "speaker": "Sol", "text": "The bakery sells sourdough."}]},
        "qa": [{"question": f"Question {i} about the garden?", "answer": "PRIVATE_GOLD_ANSWER",
                "evidence": ["D1:1"], "category": 4} for i in range(count)]}]
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _row(case="c", question="q", strategy="legacy", full=0.0):
    return Measurement(case, question, "single-hop", strategy, 100, 80, 1.0, full, full, bool(full), 0)


def test_exact_seeded_sampling_preserves_entire_history_and_excludes_labels(tmp_path):
    data = _dataset(tmp_path / "dataset.json", count=7)
    sample = load_cases("locomo", data, limit=3, seed=42)
    assert len(sample[0].questions) == 3
    assert sample == load_cases("locomo", data, limit=3, seed=42)
    assert len(sample[0].sessions[0][2]) == 2
    assert "PRIVATE_GOLD_ANSWER" not in repr(sample)
    assert all(set(message) == {"id", "speaker", "text"} for message in sample[0].sessions[0][2])


def test_unresolvable_annotations_are_retained_as_misses_instead_of_excluded(tmp_path):
    data = _dataset(tmp_path / "dataset.json", count=1)
    payload = json.loads(open(data).read())
    payload[0]["qa"][0]["evidence"] = ["D1:1", "D1:2 D4:3", "D:11:26"]
    with open(data, "w") as handle:
        json.dump(payload, handle)
    question = load_cases("locomo", data)[0].questions[0]
    assert question.evidence == frozenset({"D1:1", "D1:2 D4:3", "D:11:26"})


def test_selected_source_does_not_prove_omitted_text_or_missing_references(tmp_path):
    with Store(str(tmp_path), "evidence") as store:
        store.add("s", [{"id": "ref", "text": "The bicycle is blue. Its serial number is XYZ."}],
                  session_at="2025-01-01")
        ids = [int(row[0]) for row in store.db.execute("SELECT id FROM turns")]
        selected, full, complete, missing = source_retention(
            store, ids, "The bicycle is blue.", frozenset({"ref", "missing"}))
        assert selected == 0.5
        assert full == 0
        assert complete is False
        assert missing == 1
        assert source_retention(store, ids, "The bicycle is blue. Its serial number is XYZ.",
                                frozenset({"ref"})) == (1.0, 1.0, True, 0)
        assert source_retention(store, ids, "", frozenset()) == (None, None, None, 0)


def test_short_source_body_cannot_be_supplied_by_an_attribution_or_header(tmp_path):
    with Store(str(tmp_path), "evidence") as store:
        store.add("Lavender", [{"id": "ref", "speaker": "Lavender", "text": "Lavender"}])
        ids = [int(row[0]) for row in store.db.execute("SELECT id FROM turns")]
        assert source_retention(store, ids, "[Lavender]\nLavender:", frozenset({"ref"}))[1] == 0
        assert source_retention(store, ids, "Lavender: Lavender", frozenset({"ref"}))[1] == 1


def test_cluster_interval_resamples_conversations_and_is_deterministic():
    rows = []
    for case, score in (("one", 1.0), ("two", 0.0)):
        for question in ("a", "b", "c"):
            rows.extend([_row(case, question), _row(case, question, "coverage-v1", score)])
    comparison = clustered_delta(rows, "full_source_text_recall", resamples=100)
    assert comparison == clustered_delta(rows, "full_source_text_recall", resamples=100)
    assert comparison["paired_questions"] == 6
    assert comparison["independent_cases"] == 2
    assert comparison["candidate_minus_baseline"] == 0.5
    assert comparison["ci_lower"] == 0
    assert comparison["ci_upper"] == 1
    single = clustered_delta(rows[:6], "full_source_text_recall")
    assert single["ci_lower"] is None and single["ci_upper"] is None


def test_missing_pairs_duplicates_and_empty_evidence_are_not_fabricated_as_success():
    with pytest.raises(ValueError, match="both strategies"):
        clustered_delta([_row()], "latency_ms")
    with pytest.raises(ValueError, match="unique paired"):
        clustered_delta([_row(), _row()], "latency_ms")
    with pytest.raises(ValueError, match="unsupported"):
        clustered_delta([], "accuracy")
    rows = [replace(_row(), selected_source_recall=None, full_source_text_recall=None, complete_sources=None),
            replace(_row(strategy="coverage-v1"), selected_source_recall=None,
                    full_source_text_recall=None, complete_sources=None)]
    assert clustered_delta(rows, "full_source_text_recall")["candidate_minus_baseline"] is None


def test_mismatched_budgets_and_categories_cannot_create_a_comparison():
    with pytest.raises(ValueError, match="same budget"):
        clustered_delta([_row(), replace(_row(strategy="coverage-v1"), budget=7000)], "latency_ms")
    with pytest.raises(ValueError, match="categories"):
        clustered_delta([_row(), replace(_row(strategy="coverage-v1"), category="temporal")], "latency_ms")


@pytest.mark.parametrize("changes", [{"latency_ms": float("nan")}, {"latency_ms": -1},
                                    {"budget": True}, {"full_source_text_recall": 2},
                                    {"estimated_tokens": 101}, {"unresolved_sources": -1}])
def test_malformed_measurements_are_rejected(changes):
    with pytest.raises(ValueError):
        replace(_row(), **changes)


def test_public_evaluation_uses_real_store_without_gold_leak_or_artifacts(tmp_path, monkeypatch):
    import benchmarks.conversation_retrieval as module

    data = _dataset(tmp_path / "dataset.json", count=2)
    captured = []
    production_recall = module.recall

    def inspect(store, question, **kwargs):
        captured.append(question)
        assert "PRIVATE_GOLD_ANSWER" not in repr(list(store.db.execute("SELECT text FROM turns")))
        assert "PRIVATE_GOLD_ANSWER" not in question
        return production_recall(store, question, **kwargs)

    monkeypatch.setattr(module, "recall", inspect)
    before = set(tmp_path.iterdir())
    result = evaluate("locomo", data, budgets=[100], seed=7)
    assert set(tmp_path.iterdir()) == before
    assert len(captured) == 4
    assert result["selected_questions"] == 2
    assert result["conditions"]["judge"] is None
    assert "not provider" in result["conditions"]["token_accounting"]
    assert result["budgets"]["100"]["strategies"]["legacy"]["questions"] == 2
    assert "PRIVATE_GOLD_ANSWER" not in json.dumps(result)


@pytest.mark.parametrize("budgets", [[], [0], [True], [5, 5], [100001]])
def test_invalid_budgets_fail_before_loading_or_ingestion(budgets):
    with pytest.raises(ValueError, match="budgets"):
        evaluate("locomo", "absent-file", budgets=budgets)
