"""Changed-record work bounds and immutable, source-bound writer publication."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import threading

import pytest

from commontrace import _jsonl, fact_index, hierarchical
from commontrace.runtime_cache import RuntimeCache


@pytest.fixture(autouse=True)
def clean_retention():
    fact_index.clear_cache()
    yield
    fact_index.clear_cache()


def seed(root, count=200):
    facts = {}
    for i in range(count):
        fact = hierarchical.AtomicFact(
            id=f'fact-{i:04}', statement=f'Service rack {i} timeout is {i % 10 + 1} seconds.',
            category='constraint', scopes=['alpha'] if i % 2 else [], confidence=0.8,
            confirmations=1, valid_from='2020-01-01T00:00:00Z', valid_until=None)
        facts[fact.id] = fact
    hierarchical.save_facts(str(root), facts)
    return facts


def output(root, query, scorer='overlap-v1'):
    return [(fact.to_dict(), score) for fact, score in hierarchical.search_facts(
        str(root), query, scope='alpha', scorer=scorer)]


def assert_cold_equivalent(root):
    queries = ('', 'service timeout', 'rack 7', 'replacement')
    warm = {(scorer, query): output(root, query, scorer)
            for scorer in ('overlap-v1', 'bm25-v1') for query in queries}
    fact_index.clear_cache()
    cold = {(scorer, query): output(root, query, scorer)
            for scorer in ('overlap-v1', 'bm25-v1') for query in queries}
    assert warm == cold


def test_peek_is_non_loading_ttl_aware_and_releases_expired_weight():
    clock = [0.0]
    cache = RuntimeCache(max_entries=2, max_bytes=100, ttl=1, weigh=lambda _key, _value: 10,
                         clock=lambda: clock[0])
    assert cache.peek('missing') is None
    assert cache.get_or_load('warm', lambda: 'value') == 'value'
    stats = cache.stats()
    assert cache.peek('warm') == 'value'
    assert cache.stats() == stats
    clock[0] = 1
    assert cache.peek('warm') is None
    assert cache.stats()['bytes'] == 0


def test_peek_does_not_wait_for_an_active_loader():
    started, release = threading.Event(), threading.Event()
    cache = RuntimeCache(max_entries=2, max_bytes=100, ttl=10, weigh=lambda _key, _value: 10)
    def load():
        started.set()
        assert release.wait(5)
        return 'value'
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(cache.get_or_load, 'pending', load)
        assert started.wait(5)
        assert cache.peek('pending') is None
        release.set()
        assert future.result(timeout=5) == 'value'


def test_cold_writes_do_not_bootstrap_or_retain_an_index(tmp_path, monkeypatch):
    fact_index.clear_cache()
    def prohibited(*_args):
        raise AssertionError('cold writer must not build a query index')
    monkeypatch.setattr(fact_index, '_build', prohibited)
    seed(tmp_path)
    assert fact_index.capture_for_write(str(tmp_path)) is None
    assert fact_index.cache_info()['snapshots']['entries'] == 0


def test_over_budget_writes_do_not_build_but_withdraw_old_scoped_statistics(tmp_path, monkeypatch):
    monkeypatch.setattr(fact_index._CACHE, 'max_bytes', 1)
    seed(tmp_path, 20)
    assert output(tmp_path, 'service', 'bm25-v1')
    assert fact_index.cache_info()['snapshots']['entries'] == 0
    assert fact_index.cache_info()['statistics']['entries'] == 1
    assert fact_index.capture_for_write(str(tmp_path)) is None
    def prohibited(*_args, **_kwargs):
        raise AssertionError('over-budget writer must not materialize a new index')
    monkeypatch.setattr(fact_index, '_record', prohibited)
    hierarchical.update_fact(str(tmp_path), 'fact-0007', statement='Replacement service rack.')
    assert fact_index.cache_info()['snapshots']['entries'] == 0
    assert fact_index.cache_info()['statistics']['entries'] == 0
    assert not fact_index._STAT_KEYS
    monkeypatch.undo()
    assert output(tmp_path, 'replacement')


def test_first_canonical_mutation_only_parses_and_tokenizes_changed_record(tmp_path, monkeypatch):
    seed(tmp_path)
    output(tmp_path, 'service')
    base = fact_index.capture_for_write(str(tmp_path))
    assert base is not None
    old_payload = base.records['fact-0007'].payload
    parsed, tokenized = [], []
    original_record, original_tokens = fact_index._record, fact_index._bm25_tokens
    def recording(row, digest, previous=None):
        parsed.append(row['id'])
        return original_record(row, digest, previous)
    def tokens(text):
        tokenized.append(text)
        return original_tokens(text)
    monkeypatch.setattr(fact_index, '_record', recording)
    monkeypatch.setattr(fact_index, '_bm25_tokens', tokens)
    hierarchical.update_fact(str(tmp_path), 'fact-0007', statement='Replacement rack timeout is 9 seconds.')
    assert parsed == ['fact-0007']
    assert tokenized == ['Replacement rack timeout is 9 seconds.']
    current = fact_index.capture_for_write(str(tmp_path))
    assert current is not None and current.generation != base.generation
    assert base.records['fact-0007'].payload == old_payload
    assert current.records['fact-0008'].overlap is base.records['fact-0008'].overlap
    assert current.overlap['service'] == base.overlap['service'] - {'fact-0007'}
    with pytest.raises(fact_index.FactSnapshotChanged):
        base.ensure_current()
    monkeypatch.undo()
    assert_cold_equivalent(tmp_path)


def test_metadata_only_update_skips_all_tokenization_and_rebuilds_statistics(tmp_path, monkeypatch):
    seed(tmp_path)
    output(tmp_path, 'service', 'bm25-v1')
    old_statistics = fact_index.cache_info()['statistics']['entries']
    assert old_statistics == 1
    def prohibited(_text):
        raise AssertionError('metadata change must reuse lexical materialization')
    monkeypatch.setattr(fact_index, '_bm25_tokens', prohibited)
    hierarchical.update_fact(str(tmp_path), 'fact-0007', confidence=0.99, expires_at='2020-02-01T00:00:00Z')
    assert fact_index.cache_info()['statistics']['entries'] == 0
    assert fact_index.capture_for_write(str(tmp_path)) is not None
    monkeypatch.undo()
    assert_cold_equivalent(tmp_path)


def test_multiple_updates_additions_and_erasure_match_cold_generation(tmp_path):
    facts = seed(tmp_path, 20)
    output(tmp_path, 'service')
    facts = hierarchical.load_facts(str(tmp_path))
    facts.pop('fact-0001')
    facts['fact-0002'].statement = 'Replacement rack 7 connects a gateway.'
    facts['fact-0003'].forgotten = True
    facts['fact-0004'].confidence = 0.99
    facts['fact-0005'].valid_until = '2024-01-01T00:00:00Z'
    facts['new'] = hierarchical.AtomicFact(
        id='new', statement='Replacement rack 7 port x.', category='general', scopes=['alpha'],
        confidence=0.9, confirmations=1, valid_from='2020-01-01T00:00:00Z', valid_until=None)
    hierarchical.save_facts(str(tmp_path), facts)
    snapshot = fact_index.capture_for_write(str(tmp_path))
    assert snapshot is not None
    assert 'fact-0001' not in snapshot.records
    assert 'fact-0001' not in set().union(*snapshot.overlap.values())
    assert_cold_equivalent(tmp_path)


def test_malformed_and_nonobject_rows_preserve_cold_skip_semantics(tmp_path):
    seed(tmp_path, 20)
    output(tmp_path, 'service')
    base = fact_index.capture_for_write(str(tmp_path))
    path = hierarchical._facts_file(str(tmp_path))
    rows = tuple(json.dumps(fact.to_dict(), ensure_ascii=False)
                 for fact in hierarchical.load_facts(str(tmp_path)).values())
    rows += ('not-json', '[]', '{"id":"invalid","statement":"invalid","valid_until":"not-a-time"}')
    digest = _jsonl.write_serialized_rows(path, rows)
    assert fact_index.publish_committed(str(tmp_path), base, rows, digest)
    assert_cold_equivalent(tmp_path)


def test_unstable_legacy_row_falls_back_without_retention(tmp_path):
    seed(tmp_path, 20)
    output(tmp_path, 'service')
    base = fact_index.capture_for_write(str(tmp_path))
    rows = ('{"id":"legacy","statement":"Legacy service timeout"}',)
    digest = _jsonl.write_serialized_rows(hierarchical._facts_file(str(tmp_path)), rows)
    assert not fact_index.publish_committed(str(tmp_path), base, rows, digest)
    assert fact_index.cache_info()['snapshots']['entries'] == 0
    assert hierarchical.search_facts(str(tmp_path), 'service')
    assert fact_index.cache_info()['snapshots']['entries'] == 0


def test_supplied_digest_cannot_authorize_different_rows(tmp_path):
    seed(tmp_path, 20)
    output(tmp_path, 'service')
    base = fact_index.capture_for_write(str(tmp_path))
    rows = ('{"id":"forged","statement":"Forged service timeout","valid_from":"2020-01-01T00:00:00Z"}',)
    path = hierarchical._facts_file(str(tmp_path))
    with open(path, 'rb') as source:
        actual = hashlib.sha256(source.read()).hexdigest()
    assert not fact_index.publish_committed(str(tmp_path), base, rows, actual)
    assert 'forged' not in fact_index.snapshot_facts(str(tmp_path))


def test_literal_multiline_row_envelope_falls_back_to_actual_jsonl_reader(tmp_path):
    seed(tmp_path, 20)
    output(tmp_path, 'service')
    base = fact_index.capture_for_write(str(tmp_path))
    line = json.dumps({'id': 'one', 'statement': 'First service fact', 'valid_from': '2020-01-01T00:00:00Z'})
    another = json.dumps({'id': 'two', 'statement': 'Second service fact', 'valid_from': '2020-01-01T00:00:00Z'})
    rows = (line + '\n' + another,)
    digest = _jsonl.write_serialized_rows(hierarchical._facts_file(str(tmp_path)), rows)
    assert not fact_index.publish_committed(str(tmp_path), base, rows, digest)
    assert set(fact_index.snapshot_facts(str(tmp_path))) == {'one', 'two'}
