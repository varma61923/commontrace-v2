from __future__ import annotations

import collections
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from commontrace import frontmatter


def _parse_after_fork(result):
    value = frontmatter._parse("child: true\n")
    result.put((value, list(frontmatter._PARSED)))


@pytest.fixture
def isolated_memo(monkeypatch):
    monkeypatch.setattr(frontmatter, "_PARSED", collections.OrderedDict())
    monkeypatch.setattr(frontmatter, "_PARSED_SIZES", {})
    monkeypatch.setattr(frontmatter, "_PARSED_BYTES", 0)


def test_concurrent_readers_and_eviction_preserve_data(isolated_memo, monkeypatch):
    monkeypatch.setattr(frontmatter, "_PARSED_MAX", 2)

    def read(worker):
        for i in range(120):
            text = f"name: lesson_{i % 9}\ntags: [one, two]\n"
            result = frontmatter._parse(text)
            assert result["name"] == f"lesson_{i % 9}"
            assert result["tags"] == ["one", "two"]
            result["tags"].append(str(worker))

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(read, range(12)))
    assert len(frontmatter._PARSED) <= 2
    assert frontmatter._PARSED_BYTES == sum(frontmatter._PARSED_SIZES.values())
    assert all(value["tags"] == ["one", "two"] for value in frontmatter._PARSED.values())


def test_yaml_parsing_does_not_hold_memo_lock(isolated_memo, monkeypatch):
    entered, release = Event(), Event()
    real_load = frontmatter.load_text

    def delayed(text):
        if text == "slow: true\n":
            entered.set()
            assert release.wait(5)
        return real_load(text)

    monkeypatch.setattr(frontmatter, "load_text", delayed)
    frontmatter._parse("fast: true\n")
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(frontmatter._parse, "slow: true\n")
        try:
            assert entered.wait(5)
            fast = pool.submit(frontmatter._parse, "fast: true\n")
            assert fast.result(timeout=2) == {"fast": True}
        finally:
            release.set()
        assert slow.result(timeout=2) == {"slow": True}


def test_retained_byte_budget_accounts_for_parsed_objects(isolated_memo, monkeypatch):
    text = "values: [" + ", ".join(str(i) for i in range(100)) + "]\n"
    # A YAML collection retains far more than its short serialized source.
    budget = len(text) * 2
    monkeypatch.setattr(frontmatter, "_PARSED_MAX_BYTES", budget)
    expected = {"values": list(range(100))}
    assert frontmatter._parse(text) == expected
    assert text not in frontmatter._PARSED
    assert frontmatter._PARSED_BYTES == 0


def test_byte_pressure_evicts_oldest_entry_and_keeps_results(isolated_memo, monkeypatch):
    text = "name: first\ntags: [a, b]\n"
    frontmatter._parse(text)
    single_size = frontmatter._PARSED_BYTES
    monkeypatch.setattr(frontmatter, "_PARSED_MAX_BYTES", single_size + 20)
    latest = "name: later\ntags: [a, b]\n"
    assert frontmatter._parse(latest) == {"name": "later", "tags": ["a", "b"]}
    assert list(frontmatter._PARSED) == [latest]
    assert 0 < frontmatter._PARSED_BYTES <= frontmatter._PARSED_MAX_BYTES


def test_oversized_source_is_returned_without_retention(isolated_memo, monkeypatch):
    monkeypatch.setattr(frontmatter, "_PARSED_MAX_BYTES", 1024)
    text = "description: " + "x" * 10000 + "\n"
    assert frontmatter._parse(text)["description"] == "x" * 10000
    assert frontmatter._PARSED == {}
    assert frontmatter._PARSED_BYTES == 0


def test_null_yaml_is_memoized(isolated_memo, monkeypatch):
    calls = []
    real_load = frontmatter.load_text

    def record(text):
        calls.append(text)
        return real_load(text)

    monkeypatch.setattr(frontmatter, "load_text", record)
    assert frontmatter._parse("") is None
    assert frontmatter._parse("") is None
    assert calls == [""]


@pytest.mark.skipif(not hasattr(os, "register_at_fork"), reason="requires fork lifecycle hooks")
def test_fork_child_resets_inherited_held_lock_and_accounting(isolated_memo):
    frontmatter._parse("parent: true\n")
    context = multiprocessing.get_context("fork")
    result = context.Queue()
    child = context.Process(target=_parse_after_fork, args=(result,))
    try:
        with frontmatter._PARSED_LOCK:
            child.start()
            child.join(5)
        assert not child.is_alive(), "child inherited a permanently held parent lock"
        assert child.exitcode == 0
        assert result.get(timeout=2) == ({"child": True}, ["child: true\n"])
        assert list(frontmatter._PARSED) == ["parent: true\n"]
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)
        result.close()
        result.join_thread()
