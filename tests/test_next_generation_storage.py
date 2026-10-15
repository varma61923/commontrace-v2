"""The storage reproducer must stop a release when a measured write gate fails."""

import hashlib
import json
import sys

import pytest

from benchmarks import next_generation_storage as benchmark


@pytest.mark.parametrize(
    "size,p95,throughput,expected",
    [
        (1000000, 19.999, 2000, 0),
        (1000000, 20, 2000, 1),
        (1000000, 10, 1999.999, 1),
        (1000000, 30, 1000, 1),
        (1000, 30, 1000, 0),
    ],
)
def test_reproducer_exit_status_enforces_measured_million_fact_write_gates(
    tmp_path, monkeypatch, size, p95, throughput, expected
):
    output = tmp_path / "storage.json"
    monkeypatch.setattr(
        sys, "argv",
        ["next_generation_storage", "--sizes", str(size), "--trials", "1",
         "--legacy-max", "0", "--output", str(output)],
    )
    monkeypatch.setattr(benchmark.fact_store, "migrate", lambda root: None)
    monkeypatch.setattr(benchmark.h, "add_facts", lambda root, batch: None)
    monkeypatch.setattr(
        benchmark, "measure",
        lambda root, count, trials: {
            "facts_before_trials": count,
            "single_write_p95_ms": p95,
            "batch_facts_per_second": throughput,
        },
    )

    assert benchmark.main() == expected
    report = json.loads(output.read_text())
    gates = report["gates"]
    assert gates["single_write_p95_lt_20ms_at_1m"] is (size == 1000000 and p95 < 20)
    assert gates["batch_gte_2000_facts_per_second_at_1m"] is (size == 1000000 and throughput >= 2000)
    assert "unverified" in gates["hybrid_search_at_1m"]
    assert "not exercised" in gates["restart_zero_reembedding"]
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    assert (tmp_path / "storage.json.sha256").read_text() == checksum + "  storage.json\n"
