"""Exact ranking oracle, adversarial cancellation and real scoped-engine scans."""
from __future__ import annotations

import asyncio
import heapq
import math
import random
import struct
import sys

import pytest

from commontrace import vector_scoring
from commontrace.vector_store import SQLiteVectorIndex, VectorRecord


def blob(values):
    return struct.pack(f'<{len(values)}f', *values)


def quantize(values):
    return struct.unpack(f'<{len(values)}f', blob(values))


def oracle(rows, query, top_k):
    def scores():
        for key, packed in rows:
            stored = struct.unpack(f'<{len(query)}f', packed)
            norm = math.hypot(*stored) * math.hypot(*query)
            raw = math.fsum(a * b for a, b in zip(stored, query)) / norm
            yield key, min(1.0, max(-1.0, raw))
    return heapq.nsmallest(top_k, scores(), key=lambda row: (-row[1], row[0]))


@pytest.mark.parametrize('dimension', [1, 3, 384, 2000])
@pytest.mark.parametrize('top_k', [0, 1, 7, 200])
@pytest.mark.parametrize('native', [True, False])
def test_results_are_identical_to_original_exact_scorer(dimension, top_k, native, monkeypatch):
    if not native:
        monkeypatch.setattr(vector_scoring, '_SUMPROD', None)
    rng = random.Random(61923 + dimension)
    query = quantize([rng.uniform(-1, 1) for _ in range(dimension)])
    rows = [(f'{i:04}', blob([rng.uniform(-1, 1) for _ in range(dimension)])) for i in range(80)]
    # Exact duplicates arrive in reverse tie order; no native estimate may
    # replace the established fsum score or suppress an equal-score key.
    rows.extend([(key, blob(query)) for key in ('tie-z', 'tie-b', 'tie-a')])
    assert vector_scoring.cosine_topk(iter(rows), query, dimension=dimension, top_k=top_k) == oracle(rows, query, top_k)


@pytest.mark.parametrize('direction', [-1, 1])
def test_screening_bound_rechecks_rounding_and_tie_boundary(direction, monkeypatch):
    dimension = 384
    query = quantize([1] * dimension)
    eps = sys.float_info.epsilon
    def deliberately_noisy_dot(stored, target):
        exact = math.fsum(a * b for a, b in zip(stored, target))
        # Inject substantially more noise than actual extended-precision dots,
        # yet within the documented rejection allowance in cosine units.
        return exact + direction * 16 * (dimension + 1) * eps * math.hypot(*stored) * math.hypot(*target)
    monkeypatch.setattr(vector_scoring, '_SUMPROD', deliberately_noisy_dot)
    rows = [('z', blob(query)), ('b', blob(query)), ('a', blob(query)),
            ('near', blob([1 + 2**-23] + [1] * (dimension - 1))), ('opposite', blob([-1] * dimension))]
    assert vector_scoring.cosine_topk(rows, query, dimension=dimension, top_k=2) == oracle(rows, query, 2)


def test_dynamic_range_cancellation_and_subnormal_float32_match_exact_oracle():
    tiny = 2**-149
    huge = (2 - 2**-23) * 2**127
    query = quantize([huge, tiny, huge, tiny])
    values = [[huge, tiny, -huge, tiny], [huge, tiny, -huge, -tiny],
              [tiny, huge, tiny, -huge], [tiny] * 4, [-tiny] * 4, [huge] * 4, [-huge] * 4]
    rows = [(str(i), blob(vector)) for i, vector in enumerate(values)]
    for top_k in (1, 3, len(rows)):
        assert vector_scoring.cosine_topk(rows, query, dimension=4, top_k=top_k) == oracle(rows, query, top_k)


@pytest.mark.parametrize('corrupt', [b'wrong dimension', blob([0, 0, 0]), blob([float('nan'), 0, 0]),
                                    blob([float('inf'), 0, 0])])
def test_noncompetitive_corrupt_vectors_are_still_rejected(corrupt):
    rows = [('first', blob([1, 0, 0])), ('invalid', corrupt)]
    with pytest.raises(ValueError, match='corrupt'):
        vector_scoring.cosine_topk(rows, quantize([1, 0, 0]), dimension=3, top_k=1)


def test_native_screening_rescores_only_competitive_rows(monkeypatch):
    if vector_scoring._SUMPROD is None:
        pytest.skip('native screening requires CPython 3.12+')
    exact = math.fsum
    calls = 0
    def counting(values):
        nonlocal calls
        calls += 1
        return exact(values)
    monkeypatch.setattr(vector_scoring.math, 'fsum', counting)
    rows = [('best', blob([1, 0, 0]))] + [(str(i), blob([0, 1, 0])) for i in range(1000)]
    assert vector_scoring.cosine_topk(rows, quantize([1, 0, 0]), dimension=3, top_k=1) == [('best', 1.0)]
    assert calls == 1


def test_real_engine_freshness_filters_and_pinned_generations(tmp_path):
    async def run():
        index = await SQLiteVectorIndex.open(str(tmp_path / 'vectors.db'), tenant='owner', namespace='memory',
                                            model='model', dimension=3)
        other = await SQLiteVectorIndex.open(str(tmp_path / 'vectors.db'), tenant='foreign', namespace='memory',
                                            model='model', dimension=3)
        try:
            await index.upsert([VectorRecord('a', [1, 0, 0], 'legacy'), VectorRecord('b', [0, 1, 0], 'legacy')])
            await other.upsert([VectorRecord('foreign', [1, 0, 0])])
            assert [hit.key for hit in await index.search([1, 0, 0])] == ['a', 'b']
            assert [hit.key for hit in await index.search([1, 0, 0], allowed_ids=['b'])] == ['b']
            await index.delete(['a'])
            assert [hit.key for hit in await index.search([1, 0, 0])] == ['b']
            first = await index.snapshots.begin(1, 'first', full=True)
            assert first is not None
            await index.snapshots.stage(first, [VectorRecord('a', [1, 0, 0]), VectorRecord('b', [0, 1, 0])])
            assert await index.snapshots.publish(first)
            old = await index.snapshots.pin('first')
            second = await index.snapshots.begin(2, 'second')
            assert second is not None
            await index.snapshots.stage(second, [VectorRecord('b', [0, 0, 1])], ['a'])
            assert await index.snapshots.publish(second)
            await index.snapshots.collect_garbage()
            assert [hit.key for hit in await index.search([1, 0, 0])] == ['b']
            assert [hit.key for hit in await index.snapshots.search(old, [1, 0, 0])] == ['a', 'b']
            assert [hit.key for hit in await index.snapshots.search(old, [1, 0, 0], allowed_ids=['b'])] == ['b']
            await index.snapshots.release(old)
            assert await index.snapshots.collect_garbage() == 2
            assert [hit.key for hit in await other.search([1, 0, 0])] == ['foreign']
        finally:
            await index.close()
            await other.close()
    asyncio.run(run())


def test_antiparallel_ties_near_lower_clamp_preserve_key_order():
    query = quantize([1, 0, 0])
    rows = [('z', blob([-1, 0, 0])), ('c', blob([-1, 2**-70, 0])),
            ('b', blob([-1, -(2**-70), 0])), ('a', blob([-1, 0, 0]))]
    assert vector_scoring.cosine_topk(rows, query, dimension=3, top_k=2) == oracle(rows, query, 2)
    assert [key for key, _score in oracle(rows, query, 2)] == ['a', 'b']


@pytest.mark.parametrize('top_k', [1, 7, 25])
def test_largest_dimension_full_float32_exponent_range_matches_oracle(top_k):
    rng = random.Random(61923)
    def finite_values():
        values = []
        while len(values) < 2000:
            value = struct.unpack('<f', struct.pack('<I', rng.getrandbits(32)))[0]
            if math.isfinite(value):
                values.append(value)
        return values
    query = quantize(finite_values())
    rows = [(f'{i:04}', blob(finite_values())) for i in range(23)]
    rows.extend([('tie-z', blob(query)), ('tie-a', blob(query))])
    assert vector_scoring.cosine_topk(rows, query, dimension=2000, top_k=top_k) == oracle(rows, query, top_k)


@pytest.mark.parametrize('field,value', [('radix', 10), ('mant_dig', 24), ('max_exp', 128),
                                       ('min_exp', -125), ('rounds', 0)])
def test_nonstandard_float_configuration_forces_original_scorer(field, value, monkeypatch):
    from types import SimpleNamespace
    precision = vector_scoring.sys.float_info
    values = {name: getattr(precision, name) for name in ('radix', 'mant_dig', 'max_exp', 'min_exp', 'rounds')}
    values[field] = value
    monkeypatch.setattr(vector_scoring.sys, 'float_info', SimpleNamespace(**values))
    assert vector_scoring._native_dot() is None


def test_unaudited_future_interpreter_forces_original_scorer(monkeypatch):
    monkeypatch.setattr(vector_scoring.sys, 'version_info', (3, 15, 0))
    assert vector_scoring._native_dot() is None
