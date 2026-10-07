"""Measure real governed fact-file search and facts-channel recall; stdout only.

Run ``python -m benchmarks.fact_search --facts 10000 --trials 5``. The baseline
replays pre-index overlap selection/ranking; recall comparisons use the same
current quote assembly with that baseline search, conservatively excluding the
old second full-corpus load. Alternating trials require identical ranked scores
and recalled context. Cold builds, mutation rebuilds, broad queries and retained
cache bytes are reported separately. No timing assertion or external LLM is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import statistics
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from commontrace import fact_index, hierarchical, recall

Rows = list[tuple[hierarchical.AtomicFact, float]]


def original_search(root: str, query: str, scope: str = '', category: str = '', as_of: str | None = None,
                    limit: int = 10, include_forgotten: bool = False, show_expired: bool = False,
                    stability: str = '', *, scorer: str = 'overlap-v1') -> Rows:
    """Original public selection, ASCII overlap, scores, ties and proof checks."""
    if scorer != 'overlap-v1':
        raise ValueError('baseline implements only overlap-v1')
    candidates = hierarchical.list_facts(root, status='active', scope=scope, category=category, as_of=as_of,
                                        include_forgotten=include_forgotten, show_expired=show_expired,
                                        stability=stability)
    if not as_of:
        now = datetime.now(timezone.utc)
        candidates = [fact for fact in candidates if hierarchical._valid_at(fact, now)]
    limit = max(0, int(limit))
    query_tokens = frozenset(re.findall(r'[a-z0-9]+', query.lower()))
    if not query_tokens:
        return [(fact, fact.confidence) for fact in
                sorted(candidates, key=lambda fact: (-fact.confidence, fact.id))[:limit]]
    scored: Rows = []
    for fact in candidates:
        terms = hierarchical._fact_tokens(fact)
        overlap = len(query_tokens & terms)
        if overlap:
            scored.append((fact, round(overlap / len(query_tokens | terms) * 0.7 + fact.confidence * 0.3, 4)))
    return sorted(scored, key=lambda row: (-row[1], row[0].id))[:limit]


def seed(root: str, count: int) -> None:
    records: dict[str, hierarchical.AtomicFact] = {}
    for i in range(count):
        fact = hierarchical.AtomicFact(
            id=f'fact-{i:08}', statement=f'Service timeout policy for component{i} is {i % 30 + 1} seconds.'
                + (' Rarecalibration applies.' if i < 20 else ''),
            scopes=['alpha'] if i % 3 else [], category='constraint',
            confidence=round(0.5 + (i % 5) * 0.1, 3), confirmations=1,
            valid_from='2020-01-01T00:00:00Z', valid_until=None, stability='stable')
        records[fact.id] = fact
    hierarchical.save_facts(root, records)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _rows(rows: Rows) -> str:
    return _digest([(fact.to_dict(), score) for fact, score in rows])


def _recall(root: str, query: str, baseline: bool) -> str:
    if baseline:
        with patch.object(hierarchical, 'search_facts', original_search):
            result = recall.recall(root, query, channels=('facts',), scope='alpha', per_channel=10)
    else:
        result = recall.recall(root, query, channels=('facts',), scope='alpha', per_channel=10)
    if result.errors:
        raise RuntimeError(f'facts-channel recall failed: {result.errors}')
    return _digest(result.to_dict())


def _compare(root: str, query: str, trials: int, channel: str) -> dict[str, Any]:
    calls: dict[str, Callable[[], str]]
    if channel == 'search':
        calls = {'baseline': lambda: _rows(original_search(root, query, scope='alpha')),
                 'candidate': lambda: _rows(hierarchical.search_facts(root, query, scope='alpha'))}
    else:
        calls = {'baseline': lambda: _recall(root, query, True),
                 'candidate': lambda: _recall(root, query, False)}
    samples: dict[str, list[float]] = {name: [] for name in calls}
    checksums: dict[str, str] = {}
    for trial in range(trials + 1):
        order = ('baseline', 'candidate') if trial % 2 == 0 else ('candidate', 'baseline')
        for name in order:
            started = time.perf_counter_ns()
            digest = calls[name]()
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            if name in checksums and checksums[name] != digest:
                raise RuntimeError('stable-source output changed across trials')
            checksums[name] = digest
            if trial:
                samples[name].append(elapsed_ms)
    if checksums['baseline'] != checksums['candidate']:
        raise RuntimeError('indexed search changed exact scores, ordering or recalled context')
    medians = {name: statistics.median(values) for name, values in samples.items()}
    return {'operation': channel, 'query': query, 'trials': trials,
            'baseline_median_ms': medians['baseline'], 'candidate_median_ms': medians['candidate'],
            'speedup': medians['baseline'] / medians['candidate'],
            'scores_and_context_sha256': checksums['candidate']}


def measure(*, facts: int, trials: int) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix='commontrace-fact-benchmark-') as root:
        seed(root, facts)
        fact_index.clear_cache()
        started = time.perf_counter_ns()
        cold = hierarchical.search_facts(root, 'rarecalibration', scope='alpha')
        cold_ms = (time.perf_counter_ns() - started) / 1_000_000
        if _rows(cold) != _rows(original_search(root, 'rarecalibration', scope='alpha')):
            raise RuntimeError('cold indexing changed the original overlap oracle')
        outputs: list[dict[str, Any]] = [{'operation': 'cold-selective-search', 'elapsed_ms': cold_ms,
                                         'cache': fact_index.cache_info()}]
        for channel in ('search', 'recall'):
            for query in ('rarecalibration', 'service timeout'):
                outputs.append(_compare(root, query, trials, channel))
        started = time.perf_counter_ns()
        hierarchical.update_fact(root, 'fact-00000000', statement='Service timeout rarecalibration changed to 9 seconds.')
        mutation_ms = (time.perf_counter_ns() - started) / 1_000_000
        started = time.perf_counter_ns()
        changed = hierarchical.search_facts(root, 'rarecalibration', scope='alpha')
        rebuild_ms = (time.perf_counter_ns() - started) / 1_000_000
        if _rows(changed) != _rows(original_search(root, 'rarecalibration', scope='alpha')):
            raise RuntimeError('source mutation was not reflected in indexed output')
        outputs.append({'operation': 'source-mutation-and-rebuild', 'canonical_write_ms': mutation_ms,
                        # Preserve the old output key; a verified canonical
                        # update can now reuse its snapshot rather than rebuild.
                        'next_search_rebuild_ms': rebuild_ms, 'next_search_ms': rebuild_ms,
                        'maintenance': 'verified-snapshot-reuse-or-cold-reconciliation',
                        'cache': fact_index.cache_info()})
        started = time.perf_counter_ns()
        hierarchical.search_facts(root, 'rarecalibration', scope='alpha', scorer='bm25-v1')
        first_bm25_ms = (time.perf_counter_ns() - started) / 1_000_000
        warm_bm25: list[float] = []
        for _ in range(trials):
            started = time.perf_counter_ns()
            hierarchical.search_facts(root, 'rarecalibration', scope='alpha', scorer='bm25-v1')
            warm_bm25.append((time.perf_counter_ns() - started) / 1_000_000)
        outputs.append({'operation': 'bm25-scoped-statistics', 'first_search_ms': first_bm25_ms,
                        'warm_median_ms': statistics.median(warm_bm25), 'cache': fact_index.cache_info()})
        fact_index.clear_cache()
        return [{**output, 'facts': facts, 'python': platform.python_version(),
                 'proof_bound_facts': 0, 'scope': 'alpha'} for output in outputs]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--facts', type=int, default=10000)
    parser.add_argument('--trials', type=int, default=5)
    args = parser.parse_args()
    if not 20 <= args.facts <= 100000 or not 1 <= args.trials <= 100:
        parser.error('facts=20..100000 and trials=1..100 required')
    for output in measure(facts=args.facts, trials=args.trials):
        print(json.dumps(output, sort_keys=True))


if __name__ == '__main__':
    main()
