from __future__ import annotations

import pytest

from commontrace import hierarchical
from e2e_tests.harness.cli_runner import run_cli


def test_t2_fact_empty_statement_rejection(isolated_store: str):
    with pytest.raises(ValueError):
        hierarchical.add_fact(isolated_store, "   ")

    res = run_cli("fact", "add", "   ", dest=isolated_store)
    res.assert_failure()


def test_t2_fact_confidence_boundary_clamping(isolated_store: str):
    f_high, _ = hierarchical.add_fact(isolated_store, "High confidence fact", confidence=1.5)
    assert f_high.confidence <= 1.0, "Confidence must not exceed 1.0"

    f_low, _ = hierarchical.add_fact(isolated_store, "Low confidence fact", confidence=-0.5)
    assert f_low.confidence >= 0.0, "Confidence must not be less than 0.0"


def test_t2_fact_extreme_timestamps(isolated_store: str):
    t_past = "1970-01-01T00:00:00Z"
    t_future = "2099-12-31T23:59:59Z"

    f_past, _ = hierarchical.add_fact(
        isolated_store, "Legacy UNIX epoch assumption", valid_from=t_past, valid_until="1970-01-02T00:00:00Z"
    )
    assert f_past.valid_from is not None

    f_future, _ = hierarchical.add_fact(
        isolated_store, "Far future assumption", valid_from="2099-01-01T00:00:00Z", valid_until=t_future
    )
    assert f_future.valid_from is not None

    res_past = hierarchical.list_facts(isolated_store, status="", as_of="1970-01-01T12:00:00Z")
    assert any(f.id == f_past.id for f in res_past)
    assert not any(f.id == f_future.id for f in res_past)


def test_t2_fact_scope_sorting_dedup(isolated_store: str):
    stmt = "Redis TLS encryption is mandatory."
    f1, act1 = hierarchical.add_fact(isolated_store, stmt, scopes=["security", "infra"])
    f2, act2 = hierarchical.add_fact(isolated_store, stmt, scopes=["infra", "security"])

    assert f1.id == f2.id, "Fact IDs must be identical regardless of scope list ordering"
    assert act2 == "NOOP", "Second addition must be treated as reinforcement NOOP"


def test_t2_fact_nonexistent_supersession(isolated_store: str):
    with pytest.raises(Exception):
        hierarchical.supersede_fact(isolated_store, "fact-nonexistent-12345", "New statement")

    res = run_cli("fact", "supersede", "fact-nonexistent-12345", "New statement", dest=isolated_store)
    res.assert_failure()
