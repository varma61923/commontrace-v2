from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from commontrace.parallel import bounded_map


def test_lazy_input_does_not_run_ahead_of_the_submission_budget():
    consumed, running = [], []
    started, release = threading.Event(), threading.Event()

    def source():
        for i in range(100):
            consumed.append(i)
            yield i

    def worker(i):
        running.append(i)
        if len(running) == 2:
            started.set()
        assert release.wait(5)
        return i * 2

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(bounded_map, worker, source(), max_workers=2, max_pending=2)
        try:
            assert started.wait(5)
            assert consumed == [0, 1]
        finally:
            release.set()
        assert future.result(5) == [i * 2 for i in range(100)]


def test_failure_stops_consuming_a_large_generator():
    consumed = []

    def source():
        for i in range(1_000_000):
            consumed.append(i)
            yield i

    def fail(i):
        raise RuntimeError("worker failed")

    with pytest.raises(RuntimeError, match="worker failed"):
        bounded_map(fail, source(), max_workers=2, max_pending=3)
    assert len(consumed) == 3


def test_generator_error_is_propagated_after_workers_settle():
    completed = []

    def source():
        yield 1
        raise ValueError("input failed")

    with pytest.raises(ValueError, match="input failed"):
        bounded_map(lambda x: completed.append(x), source(), max_workers=2)
    assert completed == [1]
