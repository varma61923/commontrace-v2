"""Persisted incremental lifecycle oracle, safety and honest measurement bounds."""
from __future__ import annotations

import json
import math
from dataclasses import replace

import pytest

from benchmarks import fact_mutation
from commontrace import fact_index, hierarchical
from commontrace.fact_evidence import EvidenceResolver


@pytest.fixture(autouse=True)
def clear_indexes():
    fact_index.clear_cache()
    yield
    fact_index.clear_cache()


@pytest.mark.parametrize("field,value", [("facts", 19), ("facts", 25001), ("facts", True),
                                         ("facts", 20.5), ("trials", 0), ("trials", 21),
                                         ("trials", True), ("trials", 1.5)])
def test_measurement_limits_reject_ambiguous_or_unbounded_work(field, value):
    parameters = {"facts": 20, "trials": 1, field: value}
    with pytest.raises(ValueError):
        fact_mutation.measure(**parameters)


def test_paired_real_lifecycles_match_complete_oracle_without_a_speed_gate():
    output = fact_mutation.measure(facts=20, trials=1)
    assert output["contracts_passed"] is True, output["errors"]
    assert output["errors"] == []
    assert output["initial_persisted_facts"] == 22
    assert len(output["initial_fixture_sha256"]) == 64
    samples = output["samples"]
    assert len(samples) == len(fact_mutation.PHASES) * 2
    assert len(output["views"]) == 8
    assert tuple(output["scorers"]) == fact_mutation.SCORERS
    for phase in fact_mutation.PHASES:
        pair = [sample for sample in samples if sample["phase"] == phase]
        assert len({sample["corpus_sha256"] for sample in pair}) == 1
        assert len({sample["results_sha256"] for sample in pair}) == 1
        assert all(not sample["errors"] for sample in pair)
    for sample in samples:
        assert sample["records_after"] == sample["reused_record_objects"] + sample["constructed_record_objects"]
        assert sample["records_after"] == sample["records_before"] + sample["added_records"] - sample["removed_records"]
        assert sample["proof_bound_records"] == 2
        assert math.isfinite(sample["mutation_and_search_ms"])
        assert sample["mutation_and_search_ms"] >= 0
        assert sample["mutation_and_search_ms"] == pytest.approx(
            sample["canonical_mutation_ms"] + sample["next_selective_search_ms"])
    candidate = {sample["phase"]: sample for sample in samples if sample["variant"] == "incremental"}
    assert candidate["add"]["constructed_record_objects"] == 1
    assert candidate["statement-update"]["constructed_record_objects"] == 2
    assert candidate["metadata-update"]["constructed_record_objects"] == 1
    assert candidate["erase-source"]["removed_records"] == 1
    assert candidate["erase-source"]["constructed_record_objects"] == 0
    assert candidate["temporal-correction"]["constructed_record_objects"] == 2
    assert candidate["external-replacement"]["constructed_record_objects"] == candidate["external-replacement"]["records_after"]
    assert candidate["restart"]["constructed_record_objects"] == candidate["restart"]["records_after"]
    assert candidate["rejected-transaction"]["constructed_record_objects"] == 0
    assert candidate["no-op"]["constructed_record_objects"] == 0
    assert candidate["add"]["eligible_proof_bound_records"] == 2
    assert candidate["statement-update"]["eligible_proof_bound_records"] == 1
    assert candidate["erase-source"]["eligible_proof_bound_records"] == 0
    assert fact_index.cache_info()["snapshots"]["entries"] == 0
    assert "O(N)" in fact_mutation.__doc__
    assert "object identity" in output["record_counter"]


@pytest.mark.parametrize("scorer", fact_mutation.SCORERS)
def test_canonical_scan_checks_full_results_not_only_selective_top_k(tmp_path, scorer):
    root = str(tmp_path)
    fact_mutation.seed(root, 40)
    for phase in fact_mutation.PHASES:
        fact_mutation.mutate(root, phase)
        for view in fact_mutation.VIEWS:
            expected = fact_mutation.full_scan(root, view, scorer)
            actual = fact_mutation._search(root, view, scorer, 100)
            assert actual == expected, (phase, view.name, scorer)
            assert fact_mutation.rows_checksum(actual) == fact_mutation.rows_checksum(expected)
        assert len(fact_mutation.full_scan(root, fact_mutation.VIEWS[-1], scorer)) > 10


def test_original_fixture_is_repeatable_and_proofs_start_trusted(tmp_path):
    roots = [str(tmp_path / "left"), str(tmp_path / "right")]
    data = []
    for root in roots:
        fact_mutation.seed(root, 20)
        rows = hierarchical.load_facts(root)
        data.append({key: fact.to_dict() for key, fact in rows.items()})
        resolver = EvidenceResolver(root, rows)
        assert resolver.assess("derived").eligible
        assert resolver.assess("stale-derived").eligible
    assert data[0] == data[1]


def test_same_size_external_change_with_unchanged_revision_requires_reconciliation(tmp_path):
    root = str(tmp_path)
    fact_mutation.seed(root, 20)
    for phase in fact_mutation.PHASES[:fact_mutation.PHASES.index("external-replacement")]:
        fact_mutation.mutate(root, phase)
    before = fact_index.snapshot_facts(root)
    original = before["external"]
    before_identity = before._snapshot.generation
    fact_mutation.mutate(root, "external-replacement")
    with pytest.raises(fact_index.FactSnapshotChanged):
        before.ensure_current()
    current = fact_index.snapshot_facts(root)
    assert current._snapshot.generation != before_identity
    assert current._snapshot.generation.size == before_identity.size
    assert current["external"].revision == original.revision
    assert current["external"].statement != original.statement
    assert len(current["external"].statement.encode()) == len(original.statement.encode())
    assert fact_mutation.validate(root, "external-replacement", 100)[1] == ()


def test_rejected_write_and_noop_preserve_fact_body_checksum(tmp_path):
    root = str(tmp_path)
    fact_mutation.seed(root, 20)
    view = fact_index.snapshot_facts(root)
    original = fact_mutation.checksum({key: fact.to_dict() for key, fact in view.items()})
    for phase in ("rejected-transaction", "no-op"):
        fact_mutation.mutate(root, phase)
        loaded = hierarchical.load_facts(root)
        assert fact_mutation.checksum({key: fact.to_dict() for key, fact in loaded.items()}) == original


def test_complete_result_checksum_covers_metadata_scores_and_order(tmp_path):
    root = str(tmp_path)
    fact_mutation.seed(root, 20)
    rows = fact_mutation.full_scan(root, fact_mutation.VIEWS[0], "overlap-v1")
    original = fact_mutation.rows_checksum(rows)
    changed = [(replace(rows[0][0], source_traces=["additional-evidence"]), rows[0][1]), *rows[1:]]
    assert fact_mutation.rows_checksum(changed) != original
    assert fact_mutation.rows_checksum([(rows[0][0], rows[0][1] + .001), *rows[1:]]) != original
    assert fact_mutation.rows_checksum(list(reversed(rows))) != original


def test_oracle_baseline_does_not_consult_warm_index(tmp_path):
    root = str(tmp_path)
    fact_mutation.seed(root, 20)
    before = fact_index.cache_info()
    actual = fact_mutation.full_scan(root, fact_mutation.VIEWS[-1], "bm25-v1")
    assert actual
    assert fact_index.cache_info() == before


def test_full_scan_rejects_unknown_profile(tmp_path):
    with pytest.raises(ValueError, match="unsupported scorer"):
        fact_mutation.full_scan(str(tmp_path), fact_mutation.VIEWS[0], "invalid")


def test_cli_prints_one_json_manifest_and_uses_governance_exit_status(capsys):
    fact_mutation.main(["--facts", "20", "--trials", "1"])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    output = json.loads(captured.out)
    assert output["benchmark"] == "fact-mutation-v1"
    assert output["contracts_passed"] is True
    assert output["restart"] == "in-process index cleared; process startup is not timed"


def test_unknown_phase_does_not_change_store(tmp_path):
    root = str(tmp_path)
    fact_mutation.seed(root, 20)
    before = hierarchical.load_facts(root)
    with pytest.raises(ValueError, match="unknown mutation"):
        fact_mutation.mutate(root, "unknown")
    assert hierarchical.load_facts(root) == before


@pytest.mark.parametrize("arguments", [["--facts", "1"], ["--trials", "0"]])
def test_invalid_cli_workload_is_usage_error(arguments):
    with pytest.raises(SystemExit) as exc:
        fact_mutation.main(arguments)
    assert exc.value.code == 2
