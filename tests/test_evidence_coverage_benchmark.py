import random

import pytest

from benchmarks.evidence_coverage import reference, run
from benchmarks.task_suite import TASKS
from commontrace.conversation.coverage import assess


@pytest.mark.parametrize("question,evidence,labels", [
    ("", [], []), ("What is Ana's car serial identifier?", ["Ana has a blue car."], []),
    ("What is the retention and recovery policy?", ["Retention lasts thirty days.", "Recovery uses snapshots."], []),
    ("Who enjoys coffee?", ["I enjoy coffee."], ["Mira"]),
    ("超时 重试 café règle", ["重试策略", "超时保护", "règle du café"], []),
    ("What is Mira's favorite flower?", ["Mira likes lavender."], ["Mira"]),
])
def test_scan_oracle_matches_optimized_coverage(question, evidence, labels):
    assert assess(question, evidence, labels=labels) == reference(question, evidence, labels=labels)


def test_seeded_partial_and_missing_evidence_preserves_semantics():
    rng = random.Random(2027)
    terms = ["retention", "recovery", "launch", "serial", "port", "rack", "café", "超时"]
    for _ in range(100):
        question = " ".join(rng.sample(terms, rng.randrange(1, len(terms))))
        evidence = [" ".join(rng.sample(terms, rng.randrange(1, len(terms)))) for _ in range(rng.randrange(5))]
        assert assess(question, evidence) == reference(question, evidence)


def test_latency_report_requires_equivalent_results():
    result = run(1)
    assert result["equivalent"]
    assert result["optimized_median_ms"] > 0
    assert result["scope"].startswith("coverage gate only")


def test_task_catalog_has_seventeen_distinct_contracts():
    assert len(TASKS) == len({task for task, _, _ in TASKS}) == 17
    assert all(question and isinstance(sources, tuple) for _, question, sources in TASKS)
