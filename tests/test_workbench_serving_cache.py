"""Serving caches reuse parsing, while source edits and scoped gates stay live."""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from commontrace import frontmatter, gateway, paths, workbench


@pytest.fixture(autouse=True)
def empty_cache():
    with workbench._LESSON_CACHE_LOCK:
        workbench._LESSON_CACHE.clear()
        workbench._LESSON_CACHE_BYTES = 0
    with workbench._REVIEW_CACHE_LOCK:
        workbench._REVIEW_CACHE.clear()
    yield
    with workbench._LESSON_CACHE_LOCK:
        workbench._LESSON_CACHE.clear()
        workbench._LESSON_CACHE_BYTES = 0
    with workbench._REVIEW_CACHE_LOCK:
        workbench._REVIEW_CACHE.clear()


def lesson(root, name, *, status="active", scopes=None, rule="Use an idempotency key when retrying a write."):
    folder = paths.lessons_dir(str(root))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name + ".md")
    fm = {"name": name, "status": status, "scopes": scopes or [], "description": rule,
          "applies_when": "The payment request timed out.", "do_not_apply_when": "The request is read-only.",
          "source_traces": ["a"], "llm_draft": {"usage": {"input_tokens": 10}}}
    frontmatter.write(path, fm, "## Rule\n" + rule + "\n")
    return path


def test_paginated_handler_reads_once_and_warm_poll_does_not_decode_yaml(tmp_path, monkeypatch):
    for n in range(10):
        lesson(tmp_path, f"lesson_{n:02}")
    real = frontmatter.read
    reads = []

    def reader(path):
        reads.append(path)
        return real(path)

    monkeypatch.setattr(frontmatter, "read", reader)
    g = gateway.Gateway(str(tmp_path), token="x" * 40)
    first = g.handle("GET", "/v1/lessons?limit=3&offset=2", trusted=True)
    second = g.handle("GET", "/v1/lessons?limit=3&offset=2", trusted=True)
    assert first.status == second.status == 200
    assert first.body == second.body
    assert len(reads) == 10
    result = json.loads(first.body)
    assert result["total"] == 10 and len(result["lessons"]) == 3
    lesson(tmp_path, "lesson_new")
    third = g.handle("GET", "/v1/lessons?limit=3&offset=2", trusted=True)
    assert json.loads(third.body)["total"] == 11 and len(reads) == 11
    # Warm source data does not confer authorization on another request.
    assert g.handle("GET", "/v1/lessons?limit=3", {"Host": "localhost"}).status == 401


def test_same_size_same_mtime_edit_replacement_and_deletion_are_fresh(tmp_path):
    path = lesson(tmp_path, "lesson_live")
    first = workbench._lessons(str(tmp_path))[0]
    st = os.stat(path)
    text = open(path, encoding="utf-8").read().replace("idempotency", "replacement")
    assert len(text.encode()) == st.st_size
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert workbench._lessons(str(tmp_path))[0][2] != first[2]
    replacement = path + ".tmp"
    with open(replacement, "w", encoding="utf-8") as fh:
        fh.write(text.replace("replacement", "idempotency"))
    os.utime(replacement, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(replacement, path)
    assert workbench._lessons(str(tmp_path))[0][2] == first[2]
    os.unlink(path)
    assert workbench.count_lessons(str(tmp_path)) == 0


def test_cached_metadata_and_returned_gate_diagnostics_cannot_be_poisoned(tmp_path):
    lesson(tmp_path, "lesson_live")
    lesson(tmp_path, "lesson_review", status="review")
    rows = workbench._lessons(str(tmp_path))
    rows[0][1]["llm_draft"]["usage"]["input_tokens"] = 999
    assert workbench._lessons(str(tmp_path))[0][1]["llm_draft"]["usage"]["input_tokens"] == 10
    page = workbench.list_lessons(str(tmp_path), "review")
    assert "redundancy" in page[0]["checks"]["failed"]
    page[0]["checks"]["failed"].clear()
    assert "redundancy" in workbench.list_lessons(str(tmp_path), "review")[0]["checks"]["failed"]


def test_review_gate_cache_tracks_active_body_status_scope_and_draft_edits(tmp_path, monkeypatch):
    lesson(tmp_path, "lesson_live", scopes=["team_a"])
    draft = lesson(tmp_path, "lesson_review", status="review", scopes=["team_a", "team_b"])
    original = workbench.draft_quality.gate_failures
    calls = []

    def gates(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(workbench.draft_quality, "gate_failures", gates)
    a = workbench.list_lessons(str(tmp_path), "review", scope="team_a")
    assert "redundancy" in a[0]["checks"]["failed"]
    assert workbench.list_lessons(str(tmp_path), "review", scope="team_a") == a and len(calls) == 1
    b = workbench.list_lessons(str(tmp_path), "review", scope="team_b")
    assert "redundancy" not in b[0]["checks"]["failed"] and len(calls) == 2
    lesson(tmp_path, "lesson_live", scopes=["team_a"], rule="Record sunrise photographs from the observatory.")
    workbench.list_lessons(str(tmp_path), "review", scope="team_a")
    assert len(calls) == 3  # A changed active body recomputes the diagnostics.
    lesson(tmp_path, "lesson_live", scopes=["team_a"])
    assert "redundancy" in workbench.list_lessons(str(tmp_path), "review", scope="team_a")[0]["checks"]["failed"]
    lesson(tmp_path, "lesson_live", scopes=["team_a"], status="archived")
    assert "redundancy" not in workbench.list_lessons(str(tmp_path), "review", scope="team_a")[0]["checks"]["failed"]
    fm, body = frontmatter.read(draft)
    fm["description"] = "TODO: complete this draft"
    frontmatter.write(draft, fm, body)
    assert "scaffolding" in workbench.list_lessons(str(tmp_path), "review", scope="team_a")[0]["checks"]["failed"]


def test_cache_entry_and_serialized_content_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(workbench, "LESSON_CACHE_ENTRIES", 2)
    monkeypatch.setattr(workbench, "LESSON_CACHE_BYTES", 1000)
    monkeypatch.setattr(workbench, "REVIEW_CACHE_ENTRIES", 2)
    for n in range(6):
        lesson(tmp_path, f"lesson_{n}", status="review")
    assert len(workbench.list_lessons(str(tmp_path))) == 6
    assert len(workbench._LESSON_CACHE) <= 2
    assert workbench._LESSON_CACHE_BYTES <= 1000
    assert len(workbench._REVIEW_CACHE) == 2
    path = lesson(tmp_path, "lesson_large", rule="x" * 2000)
    assert len(workbench.list_lessons(str(tmp_path))) == 7
    assert os.path.abspath(path) not in workbench._LESSON_CACHE
    assert workbench._LESSON_CACHE_BYTES <= 1000


def test_concurrent_roots_and_mutating_callers_do_not_share_metadata(tmp_path):
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        lesson(root, "lesson_shared", scopes=[root.name])
    with ThreadPoolExecutor(max_workers=8) as pool:
        pages = list(pool.map(lambda root: workbench._lessons(str(root)), roots * 20))
    for root, page in zip(roots * 20, pages):
        assert page[0][1]["scopes"] == [root.name]
        page[0][1]["scopes"].append("poison")
    assert workbench._lessons(str(roots[0]))[0][1]["scopes"] == ["one"]
    assert workbench._lessons(str(roots[1]))[0][1]["scopes"] == ["two"]


def test_an_older_reader_cannot_publish_over_a_newer_source(tmp_path, monkeypatch):
    path = lesson(tmp_path, "lesson_live")
    real = frontmatter.read
    started, release = threading.Event(), threading.Event()
    first = True

    def reader(source):
        nonlocal first
        value = real(source)
        if first:
            first = False
            started.set()
            assert release.wait(5)
        return value

    monkeypatch.setattr(frontmatter, "read", reader)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(workbench._lessons, str(tmp_path))
        assert started.wait(5)
        try:
            lesson(tmp_path, "lesson_live", rule="Consult the new source before proceeding.")
            latest = workbench._lessons(str(tmp_path))[0][2]
        finally:
            release.set()
        assert old.result(timeout=5)[0][2] != latest
    assert workbench._lessons(str(tmp_path))[0][2] == latest
    assert workbench._LESSON_CACHE[os.path.abspath(path)][2] == latest
