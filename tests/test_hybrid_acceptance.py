from __future__ import annotations

from pathlib import Path

from benchmarks import hybrid_acceptance as benchmark


def test_synthetic_hybrid_acceptance_measures_production_api_and_fresh_restart(tmp_path):
    source = str(Path(__file__).resolve().parent.parent)
    report = benchmark.measure(source=source, count=64, trials=2, embedder=benchmark.EMBEDDER,
                               synthetic=True, run_restart=True, work_dir=str(tmp_path))

    assert report["api_path"] == "commontrace.search_recipes.search"
    assert report["measurement_class"] == "production_api_with_synthetic_adapter"
    assert report["embedding_provider_kind"] == "synthetic_adapter"
    assert report["index_build"]["projection"]["processed"] == 64
    assert report["fixture_checks"]["dense_only_id_found"]
    assert report["fixture_checks"]["lexical_only_id_found"]
    assert report["fixture_checks"]["out_of_scope_id_excluded"]
    assert report["hybrid_api"]["p95_ms"] > 0
    assert report["components_excluded_from_gate"]["dense_projection"]["p95_ms"] > 0
    assert report["restart"]["document_vectors_requested"] == 0
    assert report["restart"]["query_vectors_requested"] == 1
    assert report["restart"]["sample_cache_vectors"] == 2
    assert report["restart"]["content_hash_cache_entries"] == 64
    assert report["restart"]["status"].startswith("pass_on_fixture")
    assert report["hybrid_api"]["gate"].startswith("unverified")
    assert report["server_gate"]["status"] == "unsupported_by_current_fact_projection"


def test_hybrid_gate_requires_exact_scale_and_strict_latency_threshold():
    assert benchmark.hybrid_status(facts=999_999, p95_ms=1.0) == \
        "unverified: requires exactly 1,000,000 canonical facts"
    assert benchmark.hybrid_status(facts=1_000_000, p95_ms=49.999) == "pass"
    assert benchmark.hybrid_status(facts=1_000_000, p95_ms=50.0) == "fail"


def test_restart_gate_rejects_document_embedding_or_missing_cache():
    good = {"document_vectors_requested": 0, "sample_cache_vectors": 1,
            "content_hash_cache_entries": 1_000_000, "projection_drain": {"processed": 0}}
    assert benchmark.restart_status(good).startswith("pass_on_fixture")
    assert benchmark.restart_status(good, facts=1_000_000).startswith("pass:")
    assert benchmark.restart_status(good, facts=1_000_000, synthetic=True).startswith("pass_on_fixture")
    assert benchmark.restart_status({**good, "document_vectors_requested": 1}).startswith("fail:")
    assert benchmark.restart_status({**good, "content_hash_cache_entries": 0}).startswith("fail:")
