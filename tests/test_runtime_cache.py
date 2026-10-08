from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from commontrace.runtime_cache import RuntimeCache


def make_cache(**kwargs):
    return RuntimeCache(max_entries=kwargs.pop("max_entries", 2),
                        max_bytes=kwargs.pop("max_bytes", 20),
                        ttl=kwargs.pop("ttl", 10), weigh=lambda _k, v: len(v), **kwargs)


def test_ttl_lru_byte_limits_and_oversized_values():
    now = [100.0]
    cache = make_cache(clock=lambda: now[0])
    assert cache.get_or_load("a", lambda: "a" * 10) == "a" * 10
    cache.get_or_load("b", lambda: "b" * 10)
    cache.get_or_load("a", lambda: pytest.fail("live hit must be reused"))
    cache.get_or_load("c", lambda: "c" * 10)
    assert list(cache._entries) == ["a", "c"]
    assert cache.bytes_used == 20
    assert cache.get_or_load("huge", lambda: "x" * 21) == "x" * 21
    assert list(cache._entries) == ["a", "c"]
    now[0] += 10
    assert cache.get_or_load("a", lambda: "new") == "new"
    assert cache.bytes_used == 3


def test_same_key_coalesces_but_other_keys_do_not_wait_and_clear_stops_publication(monkeypatch):
    cache = make_cache()
    started, release, waiting = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def load():
        calls.append(1)
        started.set()
        assert release.wait(5)
        return "value"

    with ThreadPoolExecutor(3) as pool:
        owner = pool.submit(cache.get_or_load, "same", load)
        assert started.wait(5)
        flight = cache._flights["same"]
        original_wait = flight.ready.wait

        def wait():
            waiting.set()
            return original_wait(5)

        monkeypatch.setattr(flight.ready, "wait", wait)
        waiter = pool.submit(cache.get_or_load, "same", load)
        try:
            assert waiting.wait(5)
            assert pool.submit(cache.get_or_load, "other", lambda: "other").result(3) == "other"
            cache.clear()
        finally:
            release.set()
        assert owner.result(5) == waiter.result(5) == "value"
    assert len(calls) == 1 and not cache._entries


def test_failed_loader_wakes_waiters_and_allows_retry(monkeypatch):
    cache = make_cache()
    started, release, waiting = threading.Event(), threading.Event(), threading.Event()

    def fail():
        started.set()
        assert release.wait(5)
        raise RuntimeError("provider down")

    with ThreadPoolExecutor(2) as pool:
        owner = pool.submit(cache.get_or_load, "key", fail)
        assert started.wait(5)
        flight = cache._flights["key"]
        original_wait = flight.ready.wait

        def wait():
            waiting.set()
            return original_wait(5)

        monkeypatch.setattr(flight.ready, "wait", wait)
        waiter = pool.submit(cache.get_or_load, "key", fail)
        try:
            assert waiting.wait(5)
        finally:
            release.set()
        for future in (owner, waiter):
            with pytest.raises(RuntimeError, match="provider down"):
                future.result(5)
    assert cache.get_or_load("key", lambda: "recovered") == "recovered"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_child_does_not_inherit_a_locked_cache():
    cache = make_cache()
    cache.get_or_load("k", lambda: "parent")
    cache._lock.acquire()
    pid = os.fork()
    if pid == 0:
        try:
            import signal

            signal.alarm(5)
            value = cache.get_or_load("k", lambda: "child")
            os._exit(0 if value == "child" else 1)
        except BaseException:
            os._exit(2)
    cache._lock.release()
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0


@pytest.mark.parametrize("kwargs", [{"ttl": float("nan")}, {"ttl": -1}, {"max_entries": -1}, {"max_bytes": -1}])
def test_invalid_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        make_cache(**kwargs)
