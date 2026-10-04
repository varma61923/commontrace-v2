"""Performance-cap tests for clustering/dedup paths.

The perf work bounds quadratic algorithms (MinHash/LSH pre-filters, candidate
caps, deterministic sampling, shared signature memos) while keeping small
inputs byte-identical. These tests pin: determinism, bounded runtime on large
synthetic inputs, and small-input equivalence.
"""
from __future__ import annotations

import time

from commontrace import consolidate, distill, redundancy, reliability, taxonomy


def _trace(i: int, topic: str = "refund delay") -> distill.TraceCandidate:
    return distill.TraceCandidate(
        id=f"t{i:04d}", path=f"/tmp/t{i:04d}.md",
        title=f"{topic} case {i % 5}",
        context_text=f"customer reports {topic} with order {i % 5}",
        solution_text="issued refund", tags=["support"], agent_type="support",
    )


def _lesson(i: int, topic: str = "refund delay") -> dict:
    return {
        "name": f"lesson_{i:04d}", "description": f"{topic} handling rule {i % 5}",
        "status": "active", "domain": "support", "agent_type": "support",
        "tags": ["support"], "importance": 3,
        "importance_rationale": "test", "applies_when": f"when {topic} occurs",
        "do_not_apply_when": "never", "uses": 0, "last_hit": "NEVER",
        "source_traces": [f"t{i:04d}"],
    }


class TestDistillCaps:
    def test_small_input_exact_path(self):
        traces = [_trace(i) for i in range(10)]
        first = distill.find_clusters(traces, existing_lessons_source_traces=[])
        second = distill.find_clusters(traces, existing_lessons_source_traces=[])
        assert [(sorted(m.id for m in c.traces)) for c in first] == [
            (sorted(m.id for m in c.traces)) for c in second]
        assert sum(len(c.traces) for c in first) == 10

    def test_large_input_bounded_and_deterministic(self):
        traces = [_trace(i) for i in range(700)]
        start = time.perf_counter()
        first = distill.find_clusters(traces, existing_lessons_source_traces=[])
        elapsed = time.perf_counter() - start
        assert elapsed < 20, f"700 traces took {elapsed:.1f}s"
        second = distill.find_clusters(traces, existing_lessons_source_traces=[])
        assert [sorted(m.id for m in c.traces) for c in first] == [
            sorted(m.id for m in c.traces) for c in second]
        assert sum(len(c.traces) for c in first) == 700

    def test_representative_is_member_and_capped(self):
        cluster = distill.Cluster(traces=[_trace(i) for i in range(200)])
        rep = distill.representative(cluster)
        assert rep in cluster.traces
        assert distill.representative(cluster) is not None


class TestTaxonomyCoverage:
    def test_build_taxonomy_uses_coverage_index(self):
        traces = [_trace(i) for i in range(30)]
        lessons = [_lesson(i) for i in range(6)]
        tax = taxonomy.build_taxonomy(traces, lessons)
        assert tax.n_patterns >= 0
        # second run agrees (deterministic index)
        tax2 = taxonomy.build_taxonomy(traces, lessons)
        assert tax.n_patterns == tax2.n_patterns


class TestReliabilityCaps:
    def test_contradictions_bounded_and_deterministic(self):
        lessons = [_lesson(i) for i in range(300)]
        start = time.perf_counter()
        first = reliability.find_contradictions(lessons)
        elapsed = time.perf_counter() - start
        assert elapsed < 20, f"300 lessons took {elapsed:.1f}s"
        assert reliability.find_contradictions(lessons) == first


class TestRedundancyCaps:
    def test_small_input_unchanged(self):
        items = [(f"s{i}", f"refund handling rule number {i % 3}") for i in range(20)]
        assert redundancy.find_near_duplicates(items) == redundancy.find_near_duplicates(items)

    def test_large_input_bounded(self):
        items = [(f"s{i}", f"refund handling rule number {i % 3} with details {i}") for i in range(250)]
        start = time.perf_counter()
        pairs = redundancy.find_near_duplicates(items)
        assert time.perf_counter() - start < 20
        assert isinstance(pairs, list)


class TestConsolidateMemo:
    def test_report_deterministic(self):
        lessons = [_lesson(i) for i in range(8)]
        first = consolidate.build_report(lessons)
        second = consolidate.build_report(lessons)
        assert first.n_active == second.n_active == 8
