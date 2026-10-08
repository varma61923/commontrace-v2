"""Exact compact storage, bounded retention and immutable canonical reuse."""
from __future__ import annotations

import gc
import hashlib
import json
import sys
import tracemalloc
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from benchmarks.fact_search import seed
from commontrace import fact_index, hierarchical
from commontrace._stem import stem


@pytest.fixture(autouse=True)
def isolated_retention():
    fact_index.clear_cache()
    yield
    fact_index.clear_cache()


def record(statement='Service gateway timeout', **fields):
    row = {'id': 'compact', 'statement': statement, 'valid_from': '2020-01-01T00:00:00Z', **fields}
    return fact_index._record(row, fact_index._line_digest(json.dumps(row, ensure_ascii=False)))


@pytest.mark.parametrize('size', [0, 1, 127, 128, 65535, 65536, 70000, 2**64 + 17])
def test_lossless_variable_length_frequency_boundaries(size):
    expected = [('alpha', size), ('部署', 7)]
    compact = fact_index._frequencies(Counter(dict(expected)))
    assert list(compact) == expected
    assert list(compact) == expected  # No retained iterator position or mutable buffer.
    assert isinstance(compact.counts, bytes)


def test_legacy_statement_above_api_limit_retains_its_complete_frequency():
    compact = record('word ' * 70000)
    assert dict(compact.bm25)['word'] == 70000
    assert compact.length == 70000
    assert compact.copy().statement == ('word ' * 70000).strip()


@pytest.mark.parametrize('values', [set(), {'only'}, {'部署', 'alpha', 'rack7'}])
def test_compact_sets_preserve_membership_equality_and_set_algebra(values):
    compact = fact_index._compact_set(values)
    assert set(compact) == values
    assert compact == frozenset(values)
    assert frozenset(values) == compact
    assert len(compact) == len(values)
    assert 'missing' not in compact and 7 not in compact
    assert (compact & {'only', 'alpha'}) == (values & {'only', 'alpha'})
    assert (compact | {'added'}) == (values | {'added'})
    assert (compact - {'only'}) == (values - {'only'})
    if len(values) == 1:
        assert isinstance(compact.values, str)


def test_complete_unicode_payload_roundtrip_and_normalized_checksum():
    compact = record('部署流程: café rack7 🚀 — preserve the exact quote.', category='environment',
                     scopes=['私有', 'alpha'], confidence=0.875, source_traces=['trace-部署'],
                     confirmations=9, forgotten=True, stability='dynamic',
                     valid_until='2027-01-01T00:00:00+00:00', expires_at='2028-02-01T00:00:00+00:00',
                     revision='unaltered-revision', created_at='2020-01-01T00:00:00+00:00',
                     updated_at='2026-10-08T00:00:00+00:00')
    payload = compact.payload
    copied = compact.copy()
    assert json.loads(payload) == copied.to_dict()
    assert compact.payload_digest == hashlib.sha256(payload.encode('utf-8')).digest()
    assert compact.payload_size == len(payload.encode('utf-8'))
    assert copied.source_traces == ['trace-部署']
    copied.source_traces.append('mutated')
    assert compact.copy().source_traces == ['trace-部署']


@pytest.mark.parametrize('change', ['broken-stream', 'trailing-stream', 'short-length', 'long-length', 'wrong-digest'])
def test_corrupt_compressed_payload_fails_closed(change):
    original = record()
    if change == 'broken-stream':
        corrupted = replace(original, compressed_payload=b'not zlib')
    elif change == 'trailing-stream':
        corrupted = replace(original, compressed_payload=original.compressed_payload + b'trailing')
    elif change == 'short-length':
        corrupted = replace(original, payload_size=original.payload_size - 1)
    elif change == 'long-length':
        corrupted = replace(original, payload_size=original.payload_size + 1)
    else:
        corrupted = replace(original, payload_digest=b'x' * 32)
    with pytest.raises(ValueError, match='payload'):
        corrupted.copy()


def test_frozen_mapping_owns_its_table_and_accounts_actual_backing_size():
    source = {f'word-{i}': 'value' for i in range(1000)}
    compact = fact_index._FrozenMap(source)
    assert compact._table_bytes == sys.getsizeof(dict(source))
    source.clear()
    assert len(compact) == 1000
    with pytest.raises(TypeError):
        compact._data['forged'] = 'no'
    assert fact_index._retained_bytes(compact) >= compact._table_bytes + sys.getsizeof(compact)
    # Explicit dict allocations already include their table; neither rows nor
    # encoded immutable buffers may disappear from the independent walk.
    value = record('Unicode allocation 🚀 café deployment')
    assert fact_index._retained_bytes(value) >= sys.getsizeof(value.compressed_payload) + sys.getsizeof(value)


def test_snapshot_local_pooling_keeps_canonical_copy_on_write_reuse(tmp_path):
    seed(str(tmp_path), 1000)
    hierarchical.search_facts(str(tmp_path), 'rarecalibration', scope='alpha')
    base = fact_index.capture_for_write(str(tmp_path))
    assert base is not None
    first, second = base.records['fact-00000001'], base.records['fact-00000002']
    assert first.category is second.category
    assert first.scopes is second.scopes
    assert first.valid_from is second.valid_from
    assert first.bm25.counts is second.bm25.counts
    facts = hierarchical.load_facts(str(tmp_path))
    hierarchical.save_facts(str(tmp_path), facts)
    normalized = fact_index.capture_for_write(str(tmp_path))
    assert normalized is not None
    # The first normalization may replace source-digest fields; a canonical
    # no-op thereafter must retain every record and its lexical buffers.
    hierarchical.save_facts(str(tmp_path), hierarchical.load_facts(str(tmp_path)))
    stable = fact_index.capture_for_write(str(tmp_path))
    assert stable is not None
    assert all(stable.records[key] is value for key, value in normalized.records.items())
    hierarchical.update_fact(str(tmp_path), 'fact-00000000', confidence=0.99)
    changed = fact_index.capture_for_write(str(tmp_path))
    assert changed is not None and changed.records['fact-00000001'] is stable.records['fact-00000001']
    assert changed.records['fact-00000000'].overlap is stable.records['fact-00000000'].overlap
    for scorer in ('overlap-v1', 'bm25-v1'):
        warm = [(fact.to_dict(), score) for fact, score in hierarchical.search_facts(
            str(tmp_path), 'service timeout', scope='alpha', scorer=scorer)]
        fact_index.clear_cache()
        cold = [(fact.to_dict(), score) for fact, score in hierarchical.search_facts(
            str(tmp_path), 'service timeout', scope='alpha', scorer=scorer)]
        assert warm == cold


def test_50000_real_facts_retain_snapshot_and_scoped_statistics_under_unchanged_budgets(tmp_path):
    seed(str(tmp_path), 50000)
    hierarchical.search_facts(str(tmp_path), 'rarecalibration', scope='alpha', scorer='bm25-v1')
    state = fact_index.cache_info()
    assert fact_index.MAX_SNAPSHOT_BYTES == 64 * 1024 * 1024
    assert state['snapshots']['entries'] == 1 and 0 < state['snapshots']['bytes'] <= fact_index.MAX_SNAPSHOT_BYTES
    assert state['statistics']['entries'] == 1 and 0 < state['statistics']['bytes'] <= 8 * 1024 * 1024
    expected = {scorer: [(fact.to_dict(), score) for fact, score in hierarchical.search_facts(
        str(tmp_path), 'rarecalibration', scope='alpha', scorer=scorer)]
        for scorer in ('overlap-v1', 'bm25-v1')}
    for _ in range(3):
        for scorer in expected:
            assert [(fact.to_dict(), score) for fact, score in hierarchical.search_facts(
                str(tmp_path), 'rarecalibration', scope='alpha', scorer=scorer)] == expected[scorer]
    after = fact_index.cache_info()
    assert after['snapshots']['misses'] == state['snapshots']['misses']
    assert after['statistics']['misses'] == state['statistics']['misses']


def test_weigh_exceeds_independently_traced_retained_objects(tmp_path):
    seed(str(tmp_path), 3000)
    stem.cache_clear()
    gc.collect()
    tracemalloc.start()
    try:
        hierarchical.search_facts(str(tmp_path), 'rarecalibration', scope='alpha')
        # Stemming has an independently bounded process cache; free-list and
        # stemming allocations are not objects owned by the snapshot cache.
        stem.cache_clear()
        gc.collect()
        snapshot = fact_index.snapshot_facts(str(tmp_path))._snapshot
        weighed = fact_index._retained_bytes((snapshot, fact_index._PAYLOAD_DICTIONARY))
        del snapshot
        gc.collect()
        retained, _ = tracemalloc.get_traced_memory()
        fact_index.clear_cache()
        gc.collect()
        released, _ = tracemalloc.get_traced_memory()
        # Measure objects actually owned by and freed with this cache. ABC and
        # Python keyword intern tables may grow independently during the call;
        # those remain after clearing and are deliberately not counted here.
        assert 0 < retained - released <= weighed
    finally:
        tracemalloc.stop()


def test_same_instant_dates_remain_exact_with_distinct_original_offsets(tmp_path):
    row = {'id': 'offset', 'statement': 'Offset rack timeout', 'valid_from': '2020-01-01T00:00:00+02:00'}
    value = hierarchical._coerce_fact(row)
    hierarchical.save_facts(str(tmp_path), {'offset': value})
    assert hierarchical.search_facts(str(tmp_path), 'offset', as_of='2019-12-31T21:00:00Z') == []
    matched = hierarchical.search_facts(str(tmp_path), 'offset', as_of='2019-12-31T22:00:00Z')
    assert matched[0][0].valid_from == value.valid_from
    assert datetime.fromisoformat(matched[0][0].valid_from).astimezone(timezone.utc).hour == 22
