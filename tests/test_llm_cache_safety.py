from __future__ import annotations

import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from commontrace import llm, llm_cache


@pytest.fixture(autouse=True)
def clear_hot():
    llm_cache._HOT.clear()
    yield
    llm_cache._HOT.clear()


def test_cache_keys_cannot_alias_delimiters_or_namespaces():
    assert llm_cache.cache_key("a\x1fb", "c") != llm_cache.cache_key("a", "b\x1fc")
    assert llm_cache.cache_key("m", "q", namespace="one") != llm_cache.cache_key("m", "q", namespace="two")


def test_provider_endpoint_account_region_and_project_partition_completions(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_LLM_CACHE", "1")
    monkeypatch.setenv("COMMONTRACE_LLM_CACHE_PATH", str(tmp_path / "c.db"))
    calls = []

    def provider(cfg, prompt):
        calls.append(cfg)
        return f"answer-{len(calls)}", {"input_tokens": 1}

    for name in ("_call_anthropic", "_call_openai_compatible", "_call_bedrock", "_call_vertex"):
        monkeypatch.setattr(llm, name, provider)
    configs = [llm.Config("anthropic", "m", "account-one"), llm.Config("anthropic", "m", "account-two"),
               llm.Config("openai-compatible", "m", "account-one", "https://one.example/v1"),
               llm.Config("openai-compatible", "m", "account-one", "https://two.example/v1"),
               llm.Config("bedrock", "m", "", region="one", cache_namespace="account"),
               llm.Config("bedrock", "m", "", region="two", cache_namespace="account"),
               llm.Config("vertex", "m", "", region="one", project="one", cache_namespace="account"),
               llm.Config("vertex", "m", "", region="one", project="two", cache_namespace="account"),
               llm.Config("anthropic", "m", "account-one", cache_namespace="tenant-one"),
               llm.Config("anthropic", "m", "account-one", cache_namespace="tenant-two")]
    for cfg in configs:
        first = llm.complete("q", cfg)
        assert llm.complete("q", cfg) == first
    assert len(calls) == len(configs)
    with sqlite3.connect(tmp_path / "c.db") as db:
        keys = str(db.execute("SELECT key FROM cache").fetchall())
    assert "account-one" not in keys and "account-two" not in keys


def test_cloud_cache_requires_an_explicit_identity_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_LLM_CACHE", "1")
    monkeypatch.setenv("COMMONTRACE_LLM_CACHE_PATH", str(tmp_path / "c.db"))
    calls = []

    def provider(cfg, prompt):
        calls.append(1)
        return "answer", {}

    monkeypatch.setattr(llm, "_call_bedrock", provider)
    cfg = llm.Config("bedrock", "m", "", region="one")
    llm.complete("q", cfg)
    llm.complete("q", cfg)
    assert len(calls) == 2
    assert "secret-key" not in repr(llm.Config("anthropic", "m", "secret-key"))


def test_concurrent_completions_have_one_owner_and_independent_usage_objects(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_LLM_CACHE", "1")
    monkeypatch.setenv("COMMONTRACE_LLM_CACHE_PATH", str(tmp_path / "c.db"))
    started, release = threading.Event(), threading.Event()
    calls = []

    def provider(cfg, prompt):
        calls.append(1)
        started.set()
        assert release.wait(5)
        return "answer", {"input_tokens": 5}

    monkeypatch.setattr(llm, "_call_anthropic", provider)
    cfg = llm.Config("anthropic", "m", "k")
    barrier = threading.Barrier(8)

    def request():
        barrier.wait(5)
        return llm.complete("q", cfg)

    with ThreadPoolExecutor(8) as pool:
        futures = [pool.submit(request) for _ in range(8)]
        try:
            assert started.wait(5)
        finally:
            release.set()
        results = [f.result(5) for f in futures]
    assert len(calls) == 1
    results[0][1]["input_tokens"] = 999
    assert all(r[1]["input_tokens"] == 5 for r in results[1:])
    assert llm.complete("q", cfg)[1]["input_tokens"] == 5


def test_ttl_and_max_entries_and_corruption_are_enforced(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(llm_cache.time, "time", lambda: clock[0])
    cache = llm_cache.LLMCache(str(tmp_path / "c.db"), ttl=10, max_entries=2)
    for n in range(3):
        cache.set(str(n), {"text": str(n)})
        clock[0] += 1
    assert cache.get("0") is None
    assert cache.get("1")["text"] == "1"
    with cache._connect() as db:
        db.execute("UPDATE cache SET value='broken' WHERE key='1'")
    assert cache.get("1") is None
    clock[0] += 10
    assert cache.get("2") is None
    cache.set("oversize", {"text": "x" * llm_cache.MAX_VALUE_BYTES})
    assert cache.get("oversize") is None


def test_hot_cache_never_extends_disk_expiry_and_public_set_invalidates_it(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(llm_cache.time, "time", lambda: clock[0])
    cache = llm_cache.LLMCache(str(tmp_path / "c.db"), ttl=10)
    cache.set("k", {"text": "old"})
    clock[0] += 9
    assert cache.get_or_compute("k", lambda: pytest.fail("live disk hit"))["text"] == "old"
    clock[0] += 2
    assert cache.get_or_compute("k", lambda: {"text": "new"})["text"] == "new"
    cache.set("k", {"text": "replaced"})
    assert cache.get_or_compute("k", lambda: pytest.fail("updated disk hit"))["text"] == "replaced"


def test_disk_payload_bytes_are_bounded_and_oversized_entries_do_not_displace_live_ones(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(llm_cache.time, "time", lambda: clock[0])
    cache = llm_cache.LLMCache(str(tmp_path / "c.db"), max_bytes=100)
    for n in range(10):
        cache.set(str(n), {"text": "x" * 25})
        clock[0] += 1
    with cache._connect() as db:
        total = db.execute("SELECT SUM(length(CAST(value AS BLOB))) FROM cache").fetchone()[0]
    assert total <= 100
    assert cache.get("9") is not None and cache.get("0") is None
    cache.set("huge", {"text": "x" * 101})
    assert cache.get("huge") is None and cache.get("9") is not None


@pytest.mark.parametrize("kind", ["missing_parent", "invalid_database", "symlink"])
def test_cache_storage_failures_and_unsafe_files_do_not_break_completions(tmp_path, kind):
    path = tmp_path / "cache.db"
    if kind == "missing_parent":
        path = tmp_path / "parent-file" / "cache.db"
        path.parent.write_text("not a directory")
    elif kind == "invalid_database":
        path.write_text("not a database")
    else:
        target = tmp_path / "target"
        target.write_text("unchanged")
        path.symlink_to(target)
    cache = llm_cache.LLMCache(str(path))
    assert cache.get("k") is None
    cache.set("k", {"text": "answer"})
    assert cache.get_or_compute("k", lambda: {"text": "answer"}) == {"text": "answer"}
    if kind == "symlink":
        assert target.read_text() == "unchanged"


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="Linux FD accounting")
def test_connections_are_closed_immediately(tmp_path):
    cache = llm_cache.LLMCache(str(tmp_path / "c.db"))
    cache.set("k", {"text": "hello"})
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(64):
        assert cache.get("k")["text"] == "hello"
    assert len(os.listdir("/proc/self/fd")) <= before + 2


def test_old_unscoped_database_entries_are_not_served(tmp_path):
    path = tmp_path / "c.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE cache (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO cache VALUES (?, ?)", ("legacy", json.dumps({"text": "unscoped"})))
    cache = llm_cache.LLMCache(str(path))
    assert cache.get("legacy") is None
    cache.set("new", {"text": "scoped"})
    assert cache.get("new") == {"text": "scoped"}


def test_older_metadata_schema_migrates_without_losing_valid_entries(tmp_path):
    import time

    path = tmp_path / "c.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE cache (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("CREATE TABLE cache_meta (key TEXT PRIMARY KEY, created_at REAL NOT NULL)")
        db.execute("INSERT INTO cache VALUES (?, ?)", ("k", json.dumps({"text": "valid"})))
        db.execute("INSERT INTO cache_meta VALUES (?, ?)", ("k", time.time()))
    cache = llm_cache.LLMCache(str(path))
    assert cache.get("k") == {"text": "valid"}
    cache.set("next", {"text": "next"})
    assert cache.get("next") == {"text": "next"}
