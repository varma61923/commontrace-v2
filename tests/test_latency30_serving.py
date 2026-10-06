"""Verified generation reuse must still stat every leaf on every request."""
from __future__ import annotations

import mmap
import os
import select
import signal
import threading

import pytest

from commontrace import frontmatter, gateway, lesson_cache, paths


@pytest.fixture(autouse=True)
def isolated_caches():
    with lesson_cache._LISTINGS_LOCK:
        lesson_cache._LISTINGS.clear()
    with gateway._ACTIVE_CACHE_LOCK:
        gateway._ACTIVE_CACHE.clear()
    yield
    with lesson_cache._LISTINGS_LOCK:
        lesson_cache._LISTINGS.clear()


def lesson(root, name="lesson_test"):
    folder = paths.lessons_dir(str(root))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name + ".md")
    frontmatter.write(path, {"name": name, "status": "active", "scopes": ["container:alpha"],
                             "description": "Retry failed payments", "importance": 3}, "## Rule\nRetry safely.\n")
    return path


def test_verified_generation_reuses_order_but_scans_every_request(tmp_path, monkeypatch):
    first = lesson(tmp_path)
    second = lesson(tmp_path, "lesson_second")
    generation = lesson_cache._listing(str(tmp_path))
    calls = []
    original = os.scandir

    def scan(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(lesson_cache.os, "scandir", scan)
    assert lesson_cache._listing(str(tmp_path)) is generation
    assert lesson_cache._listing(str(tmp_path)) is generation
    assert len(calls) == 2
    os.unlink(second)
    replacement = lesson_cache._listing(str(tmp_path))
    assert replacement is not generation
    assert set(replacement.identities) == {first}


def test_mmap_scope_change_with_restored_mtime_refreshes_metadata(tmp_path):
    path = lesson(tmp_path)
    rows, _terms = lesson_cache.load_active_with_terms(str(tmp_path))
    first_generation = lesson_cache.source_fingerprint(str(tmp_path))
    assert rows[0][1]["scopes"] == ["container:alpha"]
    before = os.stat(path)
    with open(path, "r+b") as fh:
        with mmap.mmap(fh.fileno(), 0) as mapping:
            position = mapping.find(b"container:alpha")
            assert position >= 0
            mapping[position:position + len(b"container:alpha")] = b"container:bravo"
            mapping.flush()
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert lesson_cache.source_fingerprint(str(tmp_path)) != first_generation
    current, _terms = lesson_cache.load_active_with_terms(str(tmp_path))
    assert current[0][1]["scopes"] == ["container:bravo"]
    assert lesson_cache.filter_eligible(current, scope="container:alpha") == []


def test_public_listing_cannot_mutate_retained_snapshot(tmp_path):
    lesson(tmp_path)
    public = lesson_cache.listing(str(tmp_path))
    assert type(public) is tuple
    assert not hasattr(public, "identities")
    internal = lesson_cache._listing(str(tmp_path))
    with pytest.raises(TypeError):
        internal.identities["private"] = ()
    with pytest.raises(AttributeError):
        internal.fingerprint = ()


@pytest.mark.parametrize("budget", ["_LISTING_FILES_MAX", "_LISTING_PATH_BYTES_MAX"])
def test_oversized_listing_bypasses_retention(tmp_path, monkeypatch, budget):
    path = lesson(tmp_path)
    monkeypatch.setattr(lesson_cache, budget, 0)
    rows = lesson_cache.listing(str(tmp_path))
    assert rows[0][0] == path
    assert lesson_cache._LISTINGS == {}


def test_root_lru_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(lesson_cache, "_LISTING_ROOTS_MAX", 2)
    for n in range(3):
        root = tmp_path / str(n)
        lesson(root)
        lesson_cache.listing(str(root))
    assert len(lesson_cache._LISTINGS) == 2
    assert paths.lessons_dir(str(tmp_path / "0")) not in lesson_cache._LISTINGS


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork unavailable")
@pytest.mark.filterwarnings("ignore:This process.*multi-threaded.*:DeprecationWarning")
def test_fork_child_resets_lock_held_by_parent_thread(tmp_path):
    lesson(tmp_path)
    scope = lesson_cache.one_scan()
    scope.__enter__()
    lesson_cache.listing(str(tmp_path))
    held, release = threading.Event(), threading.Event()

    def hold():
        with lesson_cache._LISTINGS_LOCK:
            held.set()
            release.wait(timeout=10)

    worker = threading.Thread(target=hold)
    worker.start()
    assert held.wait(timeout=5)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            extra = os.path.join(paths.lessons_dir(str(tmp_path)), "lesson_child.md")
            with open(extra, "w", encoding="utf-8") as fh:
                fh.write("child observation")
            result = lesson_cache.listing(str(tmp_path))
            os.write(write_fd, b"ok" if len(result) == 2 else b"bad")
        finally:
            os._exit(0)
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 5)[0], "child blocked on inherited listing lock"
        assert os.read(read_fd, 3) == b"ok"
    finally:
        release.set()
        worker.join(timeout=5)
        os.close(read_fd)
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(child, 0)
        scope.__exit__(None, None, None)
    assert not worker.is_alive()


def test_gateway_cold_changed_and_warm_recall_use_one_verified_scan(tmp_path, monkeypatch):
    path = lesson(tmp_path)
    scans = []
    real = os.scandir

    def scan(folder):
        scans.append(folder)
        return real(folder)

    monkeypatch.setattr(lesson_cache.os, "scandir", scan)
    gateway._cached_active(str(tmp_path), frontmatter.read)
    assert len(scans) == 1
    gateway._cached_active(str(tmp_path), frontmatter.read)
    assert len(scans) == 2
    before = os.stat(path)
    text = open(path, encoding="utf-8").read()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace("alpha", "bravo"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    rows, _terms = gateway._cached_active(str(tmp_path), frontmatter.read)
    assert len(scans) == 3
    assert rows[0][1]["scopes"] == ["container:bravo"]
