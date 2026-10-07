"""Stdout benchmark verifies score identity and releases its temporary database."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest

from benchmarks.vector_search import measure
from commontrace import vector_scoring


def test_benchmark_emits_both_engines_and_workloads_without_artifacts(tmp_path):
    environment = dict(os.environ, TMPDIR=str(tmp_path))
    completed = subprocess.run([sys.executable, '-m', 'benchmarks.vector_search', '--vectors', '32',
                                '--dimension', '3', '--trials', '1', '--top-k', '3'],
                               env=environment, capture_output=True, text=True, timeout=30, check=True)
    outputs = [json.loads(line) for line in completed.stdout.splitlines()]
    assert {(row['engine'], row['workload']) for row in outputs} == {
        ('SQLite exact legacy', 'random'), ('SQLite exact MVCC', 'random'),
        ('SQLite exact legacy', 'duplicates'), ('SQLite exact MVCC', 'duplicates')}
    for workload in ('random', 'duplicates'):
        assert len({row['scores_and_ranking_sha256'] for row in outputs if row['workload'] == workload}) == 1
    assert all(row['vectors'] == 32 and row['dimension'] == 3 and row['trials'] == 1 for row in outputs)
    assert not list(tmp_path.iterdir())


def test_benchmark_rejects_changed_scores_and_always_cleans_up(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    def changed_scores(*args, **kwargs):
        return [('unrelated-result', 0.5)]
    monkeypatch.setattr(vector_scoring, 'cosine_topk', changed_scores)
    with pytest.raises(RuntimeError, match='changed exact scores'):
        asyncio.run(measure(vectors=8, dimension=3, top_k=3, trials=1, seed=61923, workload='random'))
    assert not list(tmp_path.iterdir())
