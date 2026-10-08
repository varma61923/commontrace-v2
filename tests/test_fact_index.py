"""Old ranking oracle, selective work, coherent generations and bounded retention."""
from __future__ import annotations

import concurrent.futures
import json
import re
import threading
from datetime import datetime, timezone

import pytest

from commontrace import fact_index, hierarchical


def corpus(root, size=200):
    facts = {}
    for i in range(size):
        fact = hierarchical.AtomicFact(
            id=f'fact-{i:05}', statement=f'Database quorum is {i % 9} for tenant shard {i}.',
            category='constraint' if i % 2 else 'general', scopes=['alpha'] if i % 3 else [],
            confidence=round(0.5 + (i % 5) * 0.1, 3), confirmations=1,
            valid_from='2020-01-01T00:00:00Z', valid_until=None,
            stability='stable' if i % 2 else 'dynamic')
        if i % 17 == 0:
            fact.forgotten = True
        if i % 19 == 0:
            fact.status, fact.valid_until = 'deleted', '2024-01-01T00:00:00Z'
        if i % 23 == 0:
            fact.expires_at = '2024-01-01T00:00:00Z'
        if i == size - 1:
            fact.statement = 'Rareword orbit calibration is ready.'
        facts[fact.id] = fact
    hierarchical.save_facts(str(root), facts)
    return facts


def old_search(root, query, **options):
    """Independent pre-index public selection path, including evidence governance."""
    candidates = hierarchical.list_facts(str(root), status='active', **{
        key: value for key, value in options.items() if key != 'limit'})
    if not options.get('as_of'):
        moment = datetime.now(timezone.utc)
        candidates = [fact for fact in candidates if hierarchical._valid_at(fact, moment)]
    limit = max(0, int(options.get('limit', 10)))
    query_tokens = set(re.findall('[a-z0-9]+', query.lower()))
    if not query_tokens:
        return [(fact, fact.confidence) for fact in sorted(candidates, key=lambda fact: (-fact.confidence, fact.id))[:limit]]
    scored = []
    for fact in candidates:
        terms = set(re.findall('[a-z0-9]+', fact.statement.lower()))
        overlap = len(query_tokens & terms)
        if overlap:
            scored.append((fact, round(overlap / len(query_tokens | terms) * 0.7 + fact.confidence * 0.3, 4)))
    return sorted(scored, key=lambda row: (-row[1], row[0].id))[:limit]


@pytest.mark.parametrize('query', ['database quorum 7', '', 'the', '部署流程', 'unmatchedword'])
@pytest.mark.parametrize('options', [{}, {'scope': 'alpha', 'category': 'constraint'},
                                     {'as_of': '2023-01-01T00:00:00Z'},
                                     {'include_forgotten': True, 'show_expired': True},
                                     {'stability': 'stable', 'limit': 7}])
def test_default_overlap_matches_exact_old_scores_rows_and_rank(tmp_path, query, options):
    corpus(tmp_path)
    expected = [(fact.to_dict(), score) for fact, score in old_search(tmp_path, query, **options)]
    actual = [(fact.to_dict(), score) for fact, score in hierarchical.search_facts(str(tmp_path), query, **options)]
    assert actual == expected


def test_warm_selective_queries_only_materialize_matching_results(tmp_path, monkeypatch):
    corpus(tmp_path, 1000)
    hierarchical.search_facts(str(tmp_path), 'rareword')
    original = hierarchical._coerce_fact
    coerced = []
    def counting(row):
        coerced.append(row['id'])
        return original(row)
    monkeypatch.setattr(hierarchical, '_coerce_fact', counting)
    assert len(hierarchical.search_facts(str(tmp_path), 'rareword')) == 1
    assert coerced == ['fact-00999']
    coerced.clear()
    assert len(hierarchical.search_facts(str(tmp_path), '', limit=3)) == 3
    assert len(coerced) == 3  # Blank confidence fallback also stops at its result budget.


def test_cold_build_is_singleflight_for_independent_threads(tmp_path, monkeypatch):
    corpus(tmp_path)
    fact_index.clear_cache()
    started, release = threading.Event(), threading.Event()
    original = fact_index._build
    count = 0
    def building(*args):
        nonlocal count
        count += 1
        started.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(fact_index, '_build', building)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as workers:
        futures = [workers.submit(hierarchical.search_facts, str(tmp_path), 'rareword') for _ in range(8)]
        assert started.wait(5)
        release.set()
        results = [future.result(timeout=5) for future in futures]
    assert count == 1
    assert all([(fact.id, score) for fact, score in result] ==
               [(fact.id, score) for fact, score in results[0]] for result in results)


def test_detected_source_mutation_drops_retained_old_generation(tmp_path):
    corpus(tmp_path)
    fact_index.clear_cache()
    old = fact_index.snapshot_facts(str(tmp_path))
    hierarchical.search_facts(str(tmp_path), 'database', scorer='bm25-v1')
    old_generation = old.generation
    hierarchical.update_fact(str(tmp_path), 'fact-00199', statement='NEW_VALUE orbital calibration')
    with pytest.raises(fact_index.FactSnapshotChanged):
        old.ensure_current()
    # The prior generation is withdrawn while the verified successor is warm.
    assert fact_index.cache_info()['snapshots']['entries'] == 1
    assert fact_index.cache_info()['statistics']['entries'] == 0
    fresh = fact_index.snapshot_facts(str(tmp_path))
    assert fresh.generation != old_generation
    assert 'NEW_VALUE' in fresh['fact-00199'].statement
    assert fact_index.cache_info()['snapshots']['entries'] == 1


def test_unicode_cache_weight_covers_python_string_and_nested_storage(tmp_path):
    from sys import getsizeof
    from types import MappingProxyType
    text = 'a' * 10000 + '\U0001f30f'
    value = MappingProxyType({'payload': text, 'scopes': (text,)})
    assert fact_index._retained_bytes(value) >= getsizeof(text) + getsizeof(value)
    corpus(tmp_path)
    fact_index.clear_cache()
    snapshot = fact_index.snapshot_facts(str(tmp_path))
    assert fact_index.cache_info()['snapshots']['bytes'] >= sum(
        getsizeof(record.payload) for record in snapshot._snapshot.records.values())


def test_bm25_short_numeric_identifiers_and_nonmatching_queries(tmp_path):
    seven, _ = hierarchical.add_fact(str(tmp_path), 'Rack 7 sends telemetry through port x.')
    nine, _ = hierarchical.add_fact(str(tmp_path), 'Rack 9 sends telemetry through port y.')
    assert hierarchical.search_facts(str(tmp_path), 'rack 7', scorer='bm25-v1')[0][0].id == seven.id
    assert hierarchical.search_facts(str(tmp_path), 'port y', scorer='bm25-v1')[0][0].id == nine.id
    assert hierarchical.search_facts(str(tmp_path), 'the and it', scorer='bm25-v1') == []
    assert hierarchical.search_facts(str(tmp_path), '', scorer='bm25-v1') == []
    assert hierarchical.search_facts(str(tmp_path), 'the and it') == []
    assert len(hierarchical.search_facts(str(tmp_path), '')) == 2
    assert fact_index.matched_terms('rack 7 port x', seven.statement, 'bm25-v1') == ['7', 'port', 'rack', 'x']


def test_oversized_scope_keys_do_not_escape_statistics_byte_budget(tmp_path):
    corpus(tmp_path, 20)
    fact_index.clear_cache()
    for suffix in ('a', 'b', 'c'):
        scope = 'x' * (1024 * 1024) + suffix
        hierarchical.search_facts(str(tmp_path), 'database', scope=scope, scorer='bm25-v1')
    assert fact_index.cache_info()['statistics']['entries'] == 0
    assert not fact_index._STAT_KEYS


def test_real_file_benchmark_checks_original_search_and_recalled_context():
    from benchmarks.fact_search import measure
    outputs = measure(facts=20, trials=1)
    comparisons = [row for row in outputs if row['operation'] in ('search', 'recall')]
    assert len(comparisons) == 4
    assert {row['query'] for row in comparisons} == {'rarecalibration', 'service timeout'}
    assert all(len(row['scores_and_context_sha256']) == 64 for row in comparisons)
    assert {row['operation'] for row in outputs} >= {'cold-selective-search', 'source-mutation-and-rebuild'}
    assert fact_index.cache_info()['snapshots']['entries'] == 0


def test_legacy_missing_time_rows_do_not_reuse_clock_derived_validity(tmp_path):
    path = tmp_path / 'memory' / 'facts' / 'facts.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'id': 'legacy', 'statement': 'Legacy database timeout'}) + '\n')
    fact_index.clear_cache()
    assert hierarchical.search_facts(str(tmp_path), 'database')
    assert fact_index.cache_info()['snapshots']['entries'] == 0
    assert hierarchical.search_facts(str(tmp_path), 'database', as_of='2000-01-01T00:00:00Z') == []
