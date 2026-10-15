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
