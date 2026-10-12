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
    assert legacy["report_threshold_metrics"]["examples"] == 40
    assert legacy["report_threshold_metrics"]["tp"] == 15
    assert legacy["report_threshold_metrics"]["fp"] == 10
    assert legacy["report_threshold_metrics"]["fn"] == 9
    assert legacy["apply_threshold_metrics"]["tp"] == 9
    assert legacy["apply_threshold_metrics"]["fp"] == 5
    assert legacy["apply_threshold_metrics"]["fn"] == 15
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


def test_expanded_resolution_and_edge_cases_have_source_attribution():
    data = quality.load_fixture(quality.DEFAULT_FIXTURE)
    sources = data["sources"]
    adapted_pairs = [row for row in data["entity_pairs"] if "source_ref" in row]

    assert len(data["entity_pairs"]) == 40
    assert len(adapted_pairs) == 18
    assert len({row["id"] for row in data["entity_pairs"]}) == len(data["entity_pairs"])
    assert all(row["source_ref"].split("#", 1)[0] in sources for row in adapted_pairs)
    assert all(row["source_ref"].count("#") == 1 for row in adapted_pairs)
    assert {row["stratum"] for row in adapted_pairs} >= {
        "alias",
        "semantic-paraphrase",
        "homonym-negative",
        "near-duplicate-positive",
        "near-duplicate-negative",
    }
    assert all("label_rationale" in row for row in adapted_pairs if row["duplicate"] is False and row["stratum"] == "homonym-negative")

    edge_pairs = data["edge_pairs"]
    assert len(edge_pairs) == 10
    assert all(row["source_ref"].split("#", 1)[0] in sources for row in edge_pairs)
    assert {row["expected"] for row in edge_pairs} == {"duplicate", "contradiction", "independent"}
    assert sum(row["expected"] == "duplicate" for row in edge_pairs) == 4
    assert sum(row["expected"] == "contradiction" for row in edge_pairs) == 2
    assert sum(row["expected"] == "independent" for row in edge_pairs) == 4

    report = quality.evaluate()
    assert report["entity_resolution"]["metrics"]["examples"] == len(data["entity_pairs"])
    assert report["entity_resolution"]["embedding"] == "unmeasured"
    assert "er-27" in {row["id"] for row in report["entity_resolution"]["misclassified_pairs"]}
    edge_report = report["edge_pair_resolution"]
    assert edge_report["examples"] == 10
    assert edge_report["predicted_counts"] == {
        "duplicate": 4,
        "contradiction": 0,
        "independent": 1,
        "unknown": 5,
    }
    assert edge_report["resolved_examples"] == 5
    assert edge_report["coverage"] == 0.5
    assert edge_report["resolved_accuracy"] == 1.0
    outcomes = {row["id"]: row for row in edge_report["pair_outcomes"]}
    assert outcomes["ed-09"]["predicted"] == "duplicate"
    assert outcomes["ed-08"]["predicted"] == "independent"
    assert outcomes["ed-02"]["predicted"] == "unknown"


def test_edge_pair_adapter_reports_supported_outcomes_and_preserves_unknown():
    same_pair = {"existing_fact": "Mira likes tea", "new_fact": "Mira does not like tea"}
    assert quality._edge_pair_outcome(same_pair)[0] == "contradiction"

    reversed_pair = {
        "existing_fact": "Alice manages Bob",
        "new_fact": "Alice manages Bob",
        "existing_endpoints": ["bob", "alice"],
        "new_endpoints": ["alice", "bob"],
    }
    assert quality._edge_pair_outcome(reversed_pair)[0] == "duplicate"

    other_pair = {
        "existing_fact": "Dan likes yoga",
        "new_fact": "Dan likes yoga",
        "existing_endpoints": ["dan_jones", "yoga"],
        "new_endpoints": ["dan_smith", "yoga"],
    }
    assert quality._edge_pair_outcome(other_pair) == ("independent", "different endpoint pair")

    value_change = {"existing_fact": "Alice has 2 cats", "new_fact": "Alice has 3 cats"}
    assert quality._edge_pair_outcome(value_change) == ("unknown", "numeric value conflict requires a judge")


def test_edge_pair_adapter_rejects_incomplete_endpoint_attribution():
    row = {
        "id": "partial-endpoints",
        "existing_fact": "Alice works at Acme",
        "new_fact": "Alice works at BetaCorp",
        "existing_endpoints": ["alice", "acme"],
    }
    assert quality._edge_pair_outcome(row) == ("unknown", "incomplete endpoint attribution")
