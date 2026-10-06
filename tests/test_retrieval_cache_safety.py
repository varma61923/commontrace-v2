from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from commontrace import lesson_cache, retrieval


@pytest.fixture(autouse=True)
def fresh_cache():
    retrieval._INDEX_CACHE.clear()
    yield
    retrieval._INDEX_CACHE.clear()


def corpus(label):
    rows = [(f"/{label}/lesson.md", {"name": label, "description": f"retry {label}"})]
    terms = lesson_cache.TermCache({p: lesson_cache.field_terms(fm) for p, fm in rows})
    terms.stamps = {p: (1, i) for i, (p, _fm) in enumerate(rows)}
    terms.lessons = rows
    terms.fingerprint = tuple((p, terms.stamps[p]) for p, _fm in rows)
    terms.fingerprint_hash = hash(terms.fingerprint)
    return rows, terms


def index(source):
    rows, terms = source
    return retrieval._corpus_index(rows, terms, retrieval.SCORER_ADAPTIVE)


def test_concurrent_cold_requests_share_a_single_index(monkeypatch):
    source = corpus("shared")
    start = threading.Barrier(8)
    building, release = threading.Event(), threading.Event()
    count = 0
    count_lock = threading.Lock()
    original = retrieval._build_index

    def build(*args):
        nonlocal count
        with count_lock:
            count += 1
        building.set()
        assert release.wait(5)
        return original(*args)

    def request():
        start.wait(5)
        return index(source)

    monkeypatch.setattr(retrieval, "_build_index", build)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(request) for _ in range(8)]
        assert building.wait(5)
        release.set()
        results = [future.result(timeout=5) for future in futures]
    assert count == 1
    assert all(result is results[0] for result in results)


def test_unrelated_cold_builds_do_not_hold_the_cache_lock(monkeypatch):
    first, second = corpus("first"), corpus("second")
    building, release = threading.Event(), threading.Event()
    original = retrieval._build_index

    def build(rows, terms, scorer):
        if rows is first[0]:
            building.set()
            assert release.wait(5)
        return original(rows, terms, scorer)

    monkeypatch.setattr(retrieval, "_build_index", build)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(index, first)
        try:
            assert building.wait(5)
            assert pool.submit(index, second).result(timeout=3).n_docs == 1
        finally:
            release.set()
        assert waiting.result(timeout=5).n_docs == 1


def test_eviction_uses_recent_access_not_insertion_order(monkeypatch):
    monkeypatch.setattr(retrieval, "_INDEX_CACHE_MAX", 2)
    a, b, c = corpus("a"), corpus("b"), corpus("c")
    a_index, b_index = index(a), index(b)
    assert index(a) is a_index
    index(c)
    assert index(a) is a_index
    assert index(b) is not b_index
    assert len(retrieval._INDEX_CACHE) == 2


def test_byte_budget_evicts_entries_before_count_limit(monkeypatch):
    monkeypatch.setattr(retrieval, "_index_bytes", lambda *_args: 100)
    monkeypatch.setattr(retrieval, "_INDEX_CACHE_MAX_BYTES", 250)
    for name in ("a", "b", "c", "d"):
        index(corpus(name))
        assert retrieval._INDEX_CACHE.bytes_used <= 250
    assert len(retrieval._INDEX_CACHE) == 2
    assert retrieval._INDEX_CACHE.bytes_used == 200
    retrieval._INDEX_CACHE.clear()
    assert retrieval._INDEX_CACHE.bytes_used == 0


def test_an_oversized_index_is_usable_but_not_retained(monkeypatch):
    source = corpus("oversized")
    expected = retrieval._build_index(source[0], source[1], retrieval.SCORER_ADAPTIVE)
    size = retrieval._index_bytes(source[1].fingerprint, expected)
    # Instance dictionaries can shrink as Python shares dataclass keys; leave
    # enough margin that a newly built equivalent index also exceeds the cap.
    monkeypatch.setattr(retrieval, "_INDEX_CACHE_MAX_BYTES", size // 2)
    first = index(source)
    assert first == expected
    assert index(source) == first
    assert index(source) is not first
    assert not retrieval._INDEX_CACHE
    assert retrieval._INDEX_CACHE.bytes_used == 0


def test_oversized_entry_does_not_discard_a_small_cached_entry(monkeypatch):
    small, large = corpus("small"), corpus("large")
    monkeypatch.setattr(retrieval, "_index_bytes", lambda fp, _index: 100 if fp is small[1].fingerprint else 300)
    monkeypatch.setattr(retrieval, "_INDEX_CACHE_MAX_BYTES", 200)
    small_index = index(small)
    index(large)
    assert index(small) is small_index
    assert retrieval._INDEX_CACHE.bytes_used == 100


def test_hash_collision_never_reuses_another_corpus_index():
    first, second = corpus("first"), corpus("second")
    first[1].fingerprint_hash = second[1].fingerprint_hash = 7
    first_index, second_index = index(first), index(second)
    assert first_index is not second_index
    assert "first" in first_index.postings and "second" not in first_index.postings
    assert "second" in second_index.postings and "first" not in second_index.postings
    assert index(first) == first_index
    assert retrieval._INDEX_CACHE.bytes_used == retrieval._index_bytes(first[1].fingerprint, first_index)


def test_equal_but_distinct_fingerprints_reuse_the_index():
    first, second = corpus("same"), corpus("same")
    assert first[1].fingerprint is not second[1].fingerprint
    assert index(first) is index(second)


def test_size_estimate_covers_the_actual_retained_object_graph():
    source = corpus("sizing")
    snapshot = index(source)
    pending = [source[1].fingerprint, snapshot]
    seen, actual = set(), 0
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        actual += sys.getsizeof(value)
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (tuple, list)):
            pending.extend(value)
        elif isinstance(value, retrieval._CorpusIndex):
            pending.append(vars(value))
    assert retrieval._index_bytes(source[1].fingerprint, snapshot) >= actual


def test_inflight_hash_collision_waits_then_builds_the_correct_corpus(monkeypatch):
    first, second = corpus("first"), corpus("second")
    first[1].fingerprint_hash = second[1].fingerprint_hash = 7
    original = retrieval._build_index
    building, release, waiting = threading.Event(), threading.Event(), threading.Event()

    def build(rows, terms, scorer):
        if rows is first[0]:
            building.set()
            assert release.wait(5)
        return original(rows, terms, scorer)

    monkeypatch.setattr(retrieval, "_build_index", build)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(index, first)
        assert building.wait(5)
        flight = next(iter(retrieval._INDEX_CACHE._flights.values()))
        original_wait = flight.ready.wait

        def wait(timeout=None):
            waiting.set()
            return original_wait(timeout)

        monkeypatch.setattr(flight.ready, "wait", wait)
        other = pool.submit(index, second)
        assert waiting.wait(5)
        release.set()
        assert "first" in owner.result(timeout=5).postings
        result = other.result(timeout=5)
        assert "second" in result.postings and "first" not in result.postings


def test_concurrent_eviction_preserves_the_count_and_byte_limits(monkeypatch):
    monkeypatch.setattr(retrieval, "_index_bytes", lambda *_args: 100)
    monkeypatch.setattr(retrieval, "_INDEX_CACHE_MAX_BYTES", 350)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda name: index(corpus(name)), [f"source{i}" for i in range(80)]))
    assert all(result.n_docs == 1 for result in results)
    assert len(retrieval._INDEX_CACHE) == 3
    assert retrieval._INDEX_CACHE.bytes_used == 300


def test_failed_build_wakes_waiters_and_allows_a_retry(monkeypatch):
    source = corpus("failed")
    original = retrieval._build_index
    building, release, waiting = threading.Event(), threading.Event(), threading.Event()
    error = RuntimeError("failed cold build")

    def fail(*_args):
        building.set()
        assert release.wait(5)
        raise error

    monkeypatch.setattr(retrieval, "_build_index", fail)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(index, source)
        assert building.wait(5)
        flight = next(iter(retrieval._INDEX_CACHE._flights.values()))
        original_wait = flight.ready.wait

        def wait(timeout=None):
            waiting.set()
            return original_wait(timeout)

        monkeypatch.setattr(flight.ready, "wait", wait)
        waiter = pool.submit(index, source)
        assert waiting.wait(5)
        release.set()
        for future in (owner, waiter):
            with pytest.raises(RuntimeError, match="failed cold build"):
                future.result(timeout=5)
    assert not retrieval._INDEX_CACHE
    assert not retrieval._INDEX_CACHE._flights
    monkeypatch.setattr(retrieval, "_build_index", original)
    assert index(source).n_docs == 1


def test_clear_during_a_build_prevents_late_cache_publication(monkeypatch):
    source = corpus("cleared")
    original = retrieval._build_index
    building, release = threading.Event(), threading.Event()

    def build(*args):
        building.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(retrieval, "_build_index", build)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(index, source)
        assert building.wait(5)
        retrieval._INDEX_CACHE.clear()
        release.set()
        assert pending.result(timeout=5).n_docs == 1
    assert not retrieval._INDEX_CACHE
    assert retrieval._INDEX_CACHE.bytes_used == 0


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_resets_locked_cache_and_abandoned_builds():
    source = corpus("forked")
    index(source)
    cache = retrieval._INDEX_CACHE
    cache._flights[("abandoned", 1)] = retrieval._IndexFlight(())
    read_fd, write_fd = os.pipe()
    cache._lock.acquire()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            # Resetting must work even while the inherited cache lock is held.
            empty = len(cache) == 0 and cache.bytes_used == 0 and not cache._flights
            result = index(source)
            os.write(write_fd, b"ok" if empty and result.n_docs == 1 else b"bad")
            os._exit(0)
        except BaseException:
            os.write(write_fd, b"error")
            os._exit(1)
    cache._lock.release()
    os.close(write_fd)
    try:
        # A bounded read prevents a regression from hanging the test worker.
        import select
        ready, _, _ = select.select([read_fd], [], [], 5)
        if not ready:
            import signal
            os.kill(pid, signal.SIGKILL)
        assert ready, "forked child inherited a cache lock"
        assert os.read(read_fd, 5) == b"ok"
    finally:
        os.close(read_fd)
        _, status = os.waitpid(pid, 0)
    assert status == 0
    assert len(cache) == 1  # Child reset did not affect the parent.
