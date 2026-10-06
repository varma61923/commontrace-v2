"""Source-sensitive authorization and bounded, shared event-report work."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from commontrace import corpus_bin, frontmatter, gateway, lesson_cache, paths, retrieval


@pytest.fixture(autouse=True)
def fresh_caches():
    with gateway._ACTIVE_CACHE_LOCK:
        gateway._ACTIVE_CACHE.clear()
    with gateway._BODY_CACHE_LOCK:
        gateway._BODY_CACHE.clear()
        gateway._BODY_CACHE_BYTES = 0
    lesson_cache._FAST.clear()
    lesson_cache._MEMO.clear()
    lesson_cache._SNAPSHOTS.clear()
    retrieval._INDEX_CACHE.clear()
    yield


def write_lesson(root, slug="lesson_scoped", *, scope="container:alpha", description="Payments require retry keys",
                 body="## Rule\nUse an idempotency key when retrying a payment.\n"):
    os.makedirs(paths.lessons_dir(str(root)), exist_ok=True)
    target = os.path.join(paths.lessons_dir(str(root)), slug + ".md")
    fm = {"name": slug, "status": "active", "scopes": [scope], "description": description,
          "applies_when": "Retrying a payment request", "importance": 3, "tags": ["payments"]}
    frontmatter.write(target, fm, body)
    return target


def replace_preserving_stamp(path, before, after, *, replacement=False):
    st = os.stat(path)
    text = open(path, encoding="utf-8").read()
    assert len(before) == len(after) and before in text
    changed = text.replace(before, after)
    destination = path + ".new" if replacement else path
    with open(destination, "w", encoding="utf-8") as fh:
        fh.write(changed)
    os.utime(destination, ns=(st.st_atime_ns, st.st_mtime_ns))
    if replacement:
        os.replace(destination, path)
    assert os.stat(path).st_size == st.st_size and os.stat(path).st_mtime_ns == st.st_mtime_ns


@pytest.mark.parametrize("replacement", [False, True])
def test_scope_and_body_edits_cannot_hide_behind_preserved_mtime(tmp_path, replacement):
    path = write_lesson(tmp_path)
    instance = gateway.Gateway(str(tmp_path), token="x" * 40)
    request = {"occasion_id": "one", "query": "retry payment keys"}
    headers = {"Authorization": "Bearer " + "x" * 40, "Host": "localhost", "X-Container-Tag": "alpha"}
    before = instance.handle("POST", "/v1/recall", headers, json.dumps(request).encode())
    assert json.loads(before.body)["deliver"][0]["id"] == "lesson_scoped"
    replace_preserving_stamp(path, "container:alpha", "container:bravo", replacement=replacement)
    after = instance.handle("POST", "/v1/recall", headers, json.dumps({**request, "occasion_id": "two"}).encode())
    assert after.status == 200 and json.loads(after.body)["deliver"] == []
    headers["X-Container-Tag"] = "bravo"
    allowed = instance.handle("POST", "/v1/recall", headers, json.dumps({**request, "occasion_id": "three"}).encode())
    assert json.loads(allowed.body)["deliver"][0]["id"] == "lesson_scoped"
    old_body = gateway._cached_body(path)
    replace_preserving_stamp(path, "idempotency", "replacement", replacement=replacement)
    assert gateway._cached_body(path) != old_body
    assert instance.handle("POST", "/v1/recall", {"Host": "localhost"}, json.dumps(request).encode()).status == 401


@pytest.mark.parametrize("replacement", [False, True])
def test_persisted_metadata_and_binary_index_refresh_in_a_new_process(tmp_path, replacement):
    path = write_lesson(tmp_path, description="asteroids tracking orbit")
    first, terms = lesson_cache.load_active_with_terms(str(tmp_path))
    retrieval.rank_lessons("asteroids", first, floor=0.0, term_cache=terms)
    assert os.path.exists(corpus_bin.bin_path(os.path.join(str(tmp_path), "memory", ".cache"),
                                            retrieval.SCORER_ADAPTIVE))
    replace_preserving_stamp(path, "container:alpha", "container:bravo", replacement=replacement)
    replace_preserving_stamp(path, "asteroids", "elephants", replacement=replacement)
    script = """
import json,sys
from commontrace import lesson_cache,retrieval
rows,terms=lesson_cache.load_active_with_terms(sys.argv[1])
scoped=lesson_cache.filter_eligible(rows,scope='container:alpha')
ranked=retrieval.rank_lessons('elephants',rows,floor=0.0,term_cache=terms)
print(json.dumps({'scoped':len(scoped),'description':rows[0][1]['description'],
                 'matches':[r.slug for r in ranked]}))
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True,
                            text=True, check=True, timeout=15)
    assert json.loads(result.stdout) == {"scoped": 0, "description": "elephants tracking orbit",
                                        "matches": ["lesson_scoped"]}


def test_public_listing_keeps_three_fields_and_old_binary_schema_rebuilds(tmp_path):
    path = write_lesson(tmp_path)
    rows, terms = lesson_cache.load_active_with_terms(str(tmp_path))
    assert lesson_cache.listing(str(tmp_path)) == ((path, os.stat(path).st_mtime_ns, os.stat(path).st_size),)
    retrieval.rank_lessons("payment", rows, term_cache=terms)
    file = corpus_bin.bin_path(os.path.join(str(tmp_path), "memory", ".cache"), retrieval.SCORER_ADAPTIVE)
    data = bytearray(open(file, "rb").read())
    data[4] = 2
    with open(file, "wb") as fh:
        fh.write(data)
    assert corpus_bin.load(os.path.dirname(file), retrieval.SCORER_ADAPTIVE, rows, terms.fingerprint) is None
    retrieval._INDEX_CACHE.clear()
    assert retrieval.rank_lessons("payment", rows, term_cache=terms)
    assert open(file, "rb").read()[4] == 3


def test_body_cache_caps_bytes_and_retains_one_generation_per_path(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "_BODY_CACHE_BYTES_MAX", 300)
    monkeypatch.setattr(gateway, "_BODY_CACHE_MAX", 3)
    for n in range(10):
        path = write_lesson(tmp_path, f"lesson_{n}", body="## Rule\n" + "é" * 50 + "\n")
        assert gateway._cached_body(path)
    assert len(gateway._BODY_CACHE) <= 3 and gateway._BODY_CACHE_BYTES <= 300
    path = write_lesson(tmp_path, "lesson_latest", body="## Rule\n" + "x" * 40 + "\n")
    for n in range(4):
        frontmatter.write(path, {"status": "active"}, f"## Rule\nGeneration {n}.\n")
        assert str(n) in gateway._cached_body(path)
        assert sum(key[0] == path for key in gateway._BODY_CACHE) == 1
    frontmatter.write(path, {"status": "active"}, "## Rule\n" + "x" * 400)
    assert len(gateway._cached_body(path)) > 300
    assert not any(key[0] == path for key in gateway._BODY_CACHE)
    assert gateway._BODY_CACHE_BYTES == sum(len(body.encode()) for body in gateway._BODY_CACHE.values())


def test_older_body_reader_cannot_evict_a_newer_generation(tmp_path, monkeypatch):
    path = write_lesson(tmp_path)
    started, release = threading.Event(), threading.Event()
    original = frontmatter.read_body
    first = True

    def reader(target):
        nonlocal first
        value = original(target)
        if first:
            first = False
            started.set()
            assert release.wait(5)
        return value

    monkeypatch.setattr(frontmatter, "read_body", reader)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(gateway._cached_body, path)
        assert started.wait(5)
        try:
            replace_preserving_stamp(path, "idempotency", "replacement")
            current = gateway._cached_body(path)
        finally:
            release.set()
        assert old.result(timeout=5) != current
    assert gateway._cached_body(path) == current
    assert list(gateway._BODY_CACHE.values()) == [current]


def events(root, lines):
    folder = paths.memory_dir(str(root))
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, gateway.EVENTS_NAME)
    with open(target, "wb") as fh:
        fh.write(b"\n".join(lines) + b"\n")
    return target


def test_event_tail_decodes_each_row_once_and_keeps_line_positions(tmp_path, monkeypatch):
    lines = [json.dumps({"kind": "recall", "occasion_id": n, "nested": [n]}).encode() for n in range(20)]
    lines[-2] = b"not-json"
    lines[-4] = b"[1,2,3]"
    path = events(tmp_path, lines)
    instance = gateway.Gateway(str(tmp_path))
    original = gateway.json.loads
    decoded = []

    def loads(value, *args, **kwargs):
        decoded.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(gateway.json, "loads", loads)
    one = instance._read_events(3)
    assert [e["occasion_id"] for e in one] == [17, 19]
    one[0]["nested"].append("poison")
    assert instance._read_events(3)[0]["nested"] == [17]
    assert [e["occasion_id"] for e in instance._read_events(5)] == [15, 17, 19]
    assert len(decoded) == 5
    st = os.stat(path)
    with open(path, "wb") as fh:
        fh.write(b"\n".join(lines).replace(b'"recall"', b'"update"') + b"\n")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert instance._read_events(1)[0]["kind"] == "update"
    assert len(decoded) == 6


def test_event_tail_and_report_variants_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "REPORT_CACHE_MAX", 8)
    monkeypatch.setattr(gateway, "EVENTS_TAIL_BYTES", 1500)
    monkeypatch.setattr(gateway, "EVENTS_MAX_EVENTS", 10)
    lines = [json.dumps({"kind": "recall", "occasion_id": n, "padding": "x" * 60}).encode() for n in range(100)]
    events(tmp_path, lines)
    instance = gateway.Gateway(str(tmp_path))
    for n in range(1, 100):
        instance._memoized_events(n)
        assert len(instance._memo) <= 8
    tail = instance._event_tail
    assert len(tail[1]) <= 10 and len(tail[2]) <= 10
    assert sum(len(line) for line in tail[1]) <= 1500
    assert instance._events_truncated()


def test_identical_report_misses_share_work_but_unrelated_warm_hits_do_not_wait(tmp_path):
    instance = gateway.Gateway(str(tmp_path))
    instance._memoized_flag("warm", lambda: "ready")
    started, release = threading.Event(), threading.Event()
    count = []

    def compute():
        count.append(1)
        started.set()
        assert release.wait(5)
        return {"value": 1}

    with ThreadPoolExecutor(max_workers=8) as pool:
        first = pool.submit(instance._memoized_flag, "cold", compute)
        assert started.wait(5)
        try:
            pending = [pool.submit(instance._memoized_flag, "cold", compute) for _ in range(6)]
            warm = pool.submit(instance._memoized_flag, "warm", lambda: pytest.fail("must reuse warm report"))
            assert warm.result(timeout=1) == ("ready", True)
        finally:
            release.set()
        values = [first.result(timeout=5)] + [future.result(timeout=5) for future in pending]
    assert count == [1]
    assert sum(not cached for _, cached in values) == 1
    assert all(value == {"value": 1} for value, _ in values)
    assert not instance._memo_work


def test_unscoped_recall_applies_temporal_validity_and_expiration(tmp_path):
    for name, validity in [("current", {}), ("future", {"valid_from": "9999-01-01"}),
                           ("past", {"valid_until": "0001-01-01"}),
                           ("expired", {"expires": "0001-01-01"}),
                           ("invalid", {"valid_until": "not-a-date"})]:
        path = write_lesson(tmp_path, "lesson_" + name, scope="")
        fm, body = frontmatter.read(path)
        frontmatter.write(path, {**fm, "scopes": [], **validity}, body)
    instance = gateway.Gateway(str(tmp_path), token="x" * 40)
    response = instance.handle("POST", "/v1/recall", trusted=True, body=json.dumps({
        "occasion_id": "one", "query": "payment retry keys", "top_k": 20,
    }).encode())
    assert response.status == 200
    assert [item["id"] for item in json.loads(response.body)["deliver"]] == ["lesson_current"]


def test_scoped_index_reuses_ram_without_overwriting_the_full_disk_index(tmp_path, monkeypatch):
    write_lesson(tmp_path, "lesson_alpha", scope="container:alpha")
    write_lesson(tmp_path, "lesson_bravo", scope="container:bravo")
    rows, terms = lesson_cache.load_active_with_terms(str(tmp_path))
    retrieval.rank_lessons("payment", rows, term_cache=terms)
    path = corpus_bin.bin_path(os.path.join(str(tmp_path), "memory", ".cache"), retrieval.SCORER_ADAPTIVE)
    full_index = open(path, "rb").read()
    original = retrieval._build_index
    builds = []

    def build(*args, **kwargs):
        builds.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(retrieval, "_build_index", build)
    instance = gateway.Gateway(str(tmp_path), token="x" * 40)
    headers = {"Authorization": "Bearer " + "x" * 40, "Host": "localhost", "X-Container-Tag": "alpha"}
    for n in range(3):
        response = instance.handle("POST", "/v1/recall", headers, json.dumps({
            "occasion_id": str(n), "query": "payment retry keys", "top_k": 20,
        }).encode())
        assert [item["id"] for item in json.loads(response.body)["deliver"]] == ["lesson_alpha"]
    assert builds == [1]
    assert open(path, "rb").read() == full_index


def test_source_change_between_scoped_ranking_and_body_read_is_withheld(tmp_path, monkeypatch):
    path = write_lesson(tmp_path)
    original = retrieval.rank_lessons

    def changed_after_ranking(*args, **kwargs):
        ranked = original(*args, **kwargs)
        replace_preserving_stamp(path, "container:alpha", "container:bravo")
        fm, body = frontmatter.read(path)
        frontmatter.write(path, fm, body.replace("payment", "PRIVATE"))
        return ranked

    monkeypatch.setattr(retrieval, "rank_lessons", changed_after_ranking)
    instance = gateway.Gateway(str(tmp_path), token="x" * 40)
    headers = {"Authorization": "Bearer " + "x" * 40, "Host": "localhost", "X-Container-Tag": "alpha"}
    response = instance.handle("POST", "/v1/recall", headers, json.dumps({
        "occasion_id": "one", "query": "payment retry keys",
    }).encode())
    assert response.status == 200 and json.loads(response.body)["deliver"] == []
    assert b"PRIVATE" not in response.body
