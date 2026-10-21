from __future__ import annotations

import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor

import pytest

from unimem import llm
from unimem.circuit_breaker import CircuitBreaker, CircuitOpenError


def test_open_cooldown_recovery_and_permanent_failure():
    clock = [100.0]
    breaker = CircuitBreaker(threshold=2, recovery_seconds=10, clock=lambda: clock[0])

    def failure():
        raise TimeoutError("timeout")

    transient = lambda error: isinstance(error, TimeoutError)  # noqa: E731
    for _ in range(2):
        with pytest.raises(TimeoutError):
            breaker.call(failure, transient=transient)
    with pytest.raises(CircuitOpenError) as error:
        breaker.call(lambda: pytest.fail("open circuit must not call provider"), transient=transient)
    assert error.value.retry_after == 10
    clock[0] += 10
    assert breaker.call(lambda: "healthy", transient=transient) == "healthy"
    assert breaker.call(lambda: "next", transient=transient) == "next"
    for _ in range(3):
        with pytest.raises(ValueError):
            breaker.call(lambda: int("invalid"), transient=transient)
    assert breaker.call(lambda: "still reachable", transient=transient) == "still reachable"


def test_old_success_cannot_close_a_newly_opened_circuit():
    breaker = CircuitBreaker(threshold=1)
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        assert release.wait(5)
        return "old request"

    with ThreadPoolExecutor(1) as pool:
        older = pool.submit(breaker.call, slow, transient=lambda _e: True)
        try:
            assert started.wait(5)
            with pytest.raises(ValueError):
                breaker.call(lambda: int("invalid"), transient=lambda _e: True)
        finally:
            release.set()
        assert older.result(5) == "old request"
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: "no", transient=lambda _e: True)


def test_only_one_half_open_probe_is_admitted_and_cancellation_releases_it():
    clock = [100.0]
    breaker = CircuitBreaker(threshold=1, recovery_seconds=10, clock=lambda: clock[0])
    with pytest.raises(ValueError):
        breaker.call(lambda: int("invalid"), transient=lambda _e: True)
    clock[0] += 10
    started, release = threading.Event(), threading.Event()

    def probe():
        started.set()
        assert release.wait(5)
        raise KeyboardInterrupt()

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(breaker.call, probe, transient=lambda _e: True)
        try:
            assert started.wait(5)
            with pytest.raises(CircuitOpenError):
                breaker.call(lambda: "other", transient=lambda _e: True)
        finally:
            release.set()
        with pytest.raises(KeyboardInterrupt):
            future.result(5)
    assert breaker.call(lambda: "recovered", transient=lambda _e: True) == "recovered"


def test_complete_uses_scoped_circuits_and_does_not_count_permanent_errors(monkeypatch):
    llm._CIRCUITS.clear()

    monkeypatch.delenv("UNIMEM_LLM_CACHE", raising=False)
    monkeypatch.delenv("UNIMEM_LLM_CIRCUIT_BREAKER", raising=False)
    calls = []

    def unavailable(cfg, prompt):
        calls.append(cfg.api_key)
        raise urllib.error.URLError("provider unavailable")

    monkeypatch.setattr(llm, "_call_anthropic", unavailable)
    one, other = llm.Config("anthropic", "m", "one"), llm.Config("anthropic", "m", "other")
    for _ in range(5):
        with pytest.raises(urllib.error.URLError):
            llm.complete("q", one)
    with pytest.raises(llm.LLMUnavailable, match="retry in"):
        llm.complete("q", one)
    assert calls == ["one"] * 5
    with pytest.raises(urllib.error.URLError):
        llm.complete("q", other)
    monkeypatch.setenv("UNIMEM_LLM_CIRCUIT_BREAKER", "0")
    with pytest.raises(urllib.error.URLError):
        llm.complete("q", one)
    llm._CIRCUITS.clear()


def test_classifier_failure_releases_half_open_probe():
    clock = [100.0]
    breaker = CircuitBreaker(threshold=1, recovery_seconds=10, clock=lambda: clock[0])

    def unavailable():
        raise TimeoutError("provider unavailable")

    with pytest.raises(TimeoutError):
        breaker.call(unavailable, transient=lambda _error: True)
    clock[0] += 10

    def broken_classifier(_error):
        raise ValueError("classifier failed")

    with pytest.raises(ValueError, match="classifier failed"):
        breaker.call(unavailable, transient=broken_classifier)
    # Classification did not establish health: there is still a single probe.
    assert breaker.call(lambda: "recovered", transient=lambda _error: True) == "recovered"


def test_local_cooldown_does_not_close_half_open_circuit(monkeypatch):
    from unimem.overload import Overloaded

    clock = [100.0]
    breaker = CircuitBreaker(threshold=1, recovery_seconds=10, clock=lambda: clock[0])

    def unavailable():
        raise TimeoutError("unavailable")

    with pytest.raises(TimeoutError):
        breaker.call(unavailable, transient=lambda _error: True)
    clock[0] += 10
    monkeypatch.setattr(llm._CIRCUITS, "get_or_load", lambda _key, _loader: breaker)
    monkeypatch.delenv("UNIMEM_LLM_CIRCUIT_BREAKER", raising=False)

    class Cooldown:
        def admit(self):
            raise Overloaded(10)

    monkeypatch.setattr(llm._OVERLOADS, "get_or_load", lambda _key, _loader: Cooldown())
    with pytest.raises(Overloaded):
        llm._provider_call(llm.Config("anthropic", "m", "k"), "q", lambda *_args: pytest.fail("no call"))
    # A subsequent failed recovery must reopen immediately, not after five calls.
    with pytest.raises(TimeoutError):
        breaker.call(unavailable, transient=lambda _error: True)
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: "unexpectedly healthy", transient=lambda _error: True)
