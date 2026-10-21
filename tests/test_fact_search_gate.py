"""Large-bank performance gates fail closed on cache cliffs and regressions."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from benchmarks import fact_search
from benchmarks.fact_search import check, measure
from unimem import fact_index, hierarchical


@pytest.fixture(scope="module")
def measurements() -> list[dict[str, Any]]:
    outputs = measure(facts=20, trials=1)
    # Unit tests exercise acceptance, not host scheduling at tiny corpus sizes.
    # CI separately measures both actual arms at 50,000 facts without overrides.
    for output in outputs:
        if output['operation'] in ('search', 'recall'):
            output['speedup'] = 1.05
            output['candidate_median_ms'] = output['baseline_median_ms'] / 1.05
    return outputs


def test_complete_retained_equal_output_measurements_are_accepted(measurements: list[dict[str, Any]]) -> None:
    check(measurements)


@pytest.mark.parametrize("failure", ["evicted", "over-budget", "rebuild", "slower", "missing-recall"])
def test_gate_rejects_cliff_or_regression(measurements: list[dict[str, Any]], failure: str) -> None:
    outputs = deepcopy(measurements)
    comparison = next(row for row in outputs if row['operation'] == 'search')
    if failure == 'evicted':
        comparison['cache']['snapshots']['entries'] = 0
    elif failure == 'over-budget':
        comparison['cache']['snapshots']['bytes'] = 2**63
    elif failure == 'rebuild':
        comparison['warm_snapshot_loads'] = 1
    elif failure == 'slower':
        comparison['speedup'] = 0.33
        comparison['candidate_median_ms'] = comparison['baseline_median_ms'] / 0.33
    else:
        outputs = [row for row in outputs if row['operation'] != 'recall']
    with pytest.raises(RuntimeError):
        check(outputs)


@pytest.mark.parametrize("field", ['baseline_median_ms', 'candidate_median_ms', 'speedup'])
@pytest.mark.parametrize("value", [float('nan'), float('inf'), 0, -1, True])
def test_gate_rejects_invalid_timing_values(
    measurements: list[dict[str, Any]], field: str, value: float,
) -> None:
    outputs = deepcopy(measurements)
    comparison = next(row for row in outputs if row['operation'] == 'search')
    comparison[field] = value
    with pytest.raises(RuntimeError, match='finite and positive'):
        check(outputs)


@pytest.mark.parametrize("failure", ["missing", "rebuild-snapshot", "rebuild-statistics", "evicted"])
def test_gate_rejects_bm25_cache_rebuilds(measurements: list[dict[str, Any]], failure: str) -> None:
    outputs = deepcopy(measurements)
    bm25 = next(row for row in outputs if row['operation'] == 'bm25-scoped-statistics')
    if failure == 'missing':
        outputs.remove(bm25)
    elif failure == 'rebuild-snapshot':
        bm25['warm_snapshot_loads'] = 1
    elif failure == 'rebuild-statistics':
        bm25['warm_statistics_loads'] = 1
    else:
        bm25['cache']['statistics']['entries'] = 0
    with pytest.raises(RuntimeError):
        check(outputs)


def test_measurement_rejects_a_changed_source_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = iter(('initial-source', 'changed-source'))
    monkeypatch.setattr(fact_search, '_implementation_sha256', lambda: next(versions))
    with pytest.raises(RuntimeError, match='source changed'):
        measure(facts=20, trials=1)


@pytest.mark.parametrize("channel", ['search', 'recall'])
def test_each_comparison_warms_a_fresh_generation_instead_of_inheriting_old_expiry(
    tmp_path, monkeypatch: pytest.MonkeyPatch, channel: str,
) -> None:
    now = [0.0]
    monkeypatch.setattr(fact_index._CACHE, '_clock', lambda: now[0])
    root = str(tmp_path)
    fact_search.seed(root, 20)
    fact_index.clear_cache()
    try:
        hierarchical.search_facts(root, 'rarecalibration', scope='alpha')
        before = fact_index.cache_info()['snapshots']['misses']
        now[0] = 280.0
        original = fact_search.original_search

        def slow_baseline(*args, **kwargs):
            now[0] += 10.0
            return original(*args, **kwargs)

        monkeypatch.setattr(fact_search, 'original_search', slow_baseline)
        output = fact_search._compare(root, 'rarecalibration', 2, channel)
        assert now[0] == 310.0  # The inherited generation would have expired.
        assert output['warm_snapshot_loads'] == 0
        assert fact_index.cache_info()['snapshots']['misses'] - before == 1
    finally:
        fact_index.clear_cache()


def test_measured_comparison_still_counts_expiration_within_its_own_block(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(fact_index._CACHE, '_clock', lambda: now[0])
    root = str(tmp_path)
    fact_search.seed(root, 20)
    original = fact_search.original_search

    def slow_baseline(*args, **kwargs):
        now[0] += 180.0
        return original(*args, **kwargs)

    monkeypatch.setattr(fact_search, 'original_search', slow_baseline)
    try:
        output = fact_search._compare(root, 'rarecalibration', 2, 'search')
        assert now[0] == 540.0
        assert output['warm_snapshot_loads'] == 1
    finally:
        fact_index.clear_cache()
