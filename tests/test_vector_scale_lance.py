"""The optional scale harness measures real retrieval and cleans its own store."""
import asyncio
import os

import pytest


def test_lance_scale_records_paired_recall_and_cleans_temporary_store(tmp_path):
    if os.environ.get("COMMONTRACE_TEST_LANCE") != "1":
        pytest.skip("requires the optional LanceDB engine")
    from benchmarks import vector_scale_bench as benchmark

    report = asyncio.run(benchmark.run_lance(
        n=512, dim=8, queries=5, top_k=3, clusters=8, spread=0.05,
        seed=61923, batch=256, keep=False, work_dir=str(tmp_path),
    ))
    assert report["n"] == 512
    assert len(report["exact_samples_ms"]) == len(report["sweep"][0]["samples_ms"]) == 5
    assert 0 <= report["sweep"][0]["recall_at_k"] <= 1
    assert all(value >= 0 for value in report["exact_samples_ms"])
    assert report["source_files_sha256"]["commontrace/vector_lance.py"]
    assert report["hybrid_gate"] == "unverified: vector component only"
    assert not list(tmp_path.iterdir())


def test_lance_scale_rejects_invalid_workload_before_creating_store(tmp_path):
    if os.environ.get("COMMONTRACE_TEST_LANCE") != "1":
        pytest.skip("requires the optional LanceDB engine")
    from benchmarks import vector_scale_bench as benchmark

    with pytest.raises(ValueError, match="workload"):
        asyncio.run(benchmark.run_lance(
            n=0, dim=8, queries=5, top_k=3, clusters=8, spread=0.05,
            seed=61923, batch=256, keep=False, work_dir=str(tmp_path),
        ))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("n,p95,recall,passed", [
    (1_000_000, 49.99, .97, True),
    (1_000_000, 50., .97, False),
    (1_000_000, 10., .969, False),
    (10_000, 10., 1., False),
    (1_000_000, 10., float("nan"), False),
])
def test_lance_component_gate_preserves_scale_latency_and_recall_thresholds(n, p95, recall, passed):
    if os.environ.get("COMMONTRACE_TEST_LANCE") != "1":
        pytest.skip("requires the optional LanceDB engine")
    from benchmarks import vector_scale_bench as benchmark

    report = {"n": n, "sweep": [{"p95_ms": p95, "recall_at_k": recall}]}
    assert all(benchmark.lance_component_gates(report).values()) is passed
