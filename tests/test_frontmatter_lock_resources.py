from __future__ import annotations

import errno
import multiprocessing
import os
import time
from types import SimpleNamespace

import pytest

from commontrace import frontmatter


def _hold_lock(path, ready, release):
    with frontmatter.locked(path):
        ready.set()
        if not release.wait(10):
            raise RuntimeError("test failed to release lock")


@pytest.mark.skipif(frontmatter.fcntl is None, reason="requires POSIX file locks")
def test_contended_lock_times_out_closes_fd_and_preserves_owner(tmp_path, monkeypatch):
    path = str(tmp_path / "lesson.md")
    ready, release = multiprocessing.Event(), multiprocessing.Event()
    owner = multiprocessing.Process(target=_hold_lock, args=(path, ready, release))
    owner.start()
    try:
        assert ready.wait(5)
        inode = os.stat(path + ".lock").st_ino
        opened = []
        real_open = os.open

        def record_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        monkeypatch.setattr(frontmatter.os, "open", record_open)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="frontmatter lock"):
            with frontmatter.locked(path, timeout=0.04):
                pytest.fail("entered a lock held by another process")
        elapsed = time.monotonic() - started
        assert 0.025 <= elapsed < 2
        assert os.stat(path + ".lock").st_ino == inode
        for fd in opened:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(fd)
        # The timed-out contender must neither remove nor release the owner lock.
        with pytest.raises(TimeoutError):
            with frontmatter.locked(path, timeout=0):
                pytest.fail("the first timeout released another process's lock")
    finally:
        release.set()
        owner.join(5)
        if owner.is_alive():
            owner.terminate()
            owner.join(5)
    assert owner.exitcode == 0
    with frontmatter.locked(path, timeout=0):
        pass


@pytest.mark.parametrize("failure", [OSError(errno.EIO, "I/O error"), KeyboardInterrupt()])
def test_acquisition_failure_closes_descriptor(tmp_path, monkeypatch, failure):
    opened = []
    real_open = os.open

    def record_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def fail(*args):
        raise failure

    monkeypatch.setattr(frontmatter.os, "open", record_open)
    monkeypatch.setattr(frontmatter, "_lock_exclusive", fail)
    with pytest.raises(type(failure)):
        with frontmatter.locked(str(tmp_path / "lesson.md"), timeout=0):
            pytest.fail("failed lock acquisition entered critical section")
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("-inf"), float("nan")])
def test_invalid_timeout_has_no_filesystem_side_effects(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout"):
        with frontmatter.locked(str(tmp_path / "missing" / "lesson.md"), timeout=timeout):
            pass
    assert list(tmp_path.iterdir()) == []


def test_windows_permanent_lock_error_is_not_retried(tmp_path, monkeypatch):
    calls = []

    def fail(fd, mode, length):
        calls.append((fd, mode, length))
        raise OSError(errno.EBADF, "invalid lock descriptor")

    monkeypatch.setattr(frontmatter, "fcntl", None)
    monkeypatch.setattr(frontmatter, "msvcrt", SimpleNamespace(LK_NBLCK=2, locking=fail))
    with pytest.raises(OSError, match="invalid lock descriptor"):
        with frontmatter.locked(str(tmp_path / "lesson.md"), timeout=1):
            pass
    assert len(calls) == 1
    assert calls[0][1:] == (2, 1)
    with pytest.raises(OSError):
        os.fstat(calls[0][0])


@pytest.mark.skipif(frontmatter.fcntl is None, reason="requires POSIX inode identity checks")
def test_replaced_lock_inode_is_retried_and_previous_descriptor_closed(tmp_path, monkeypatch):
    lock_path = str(tmp_path / "lesson.md.lock")
    real_lock = frontmatter._lock_exclusive
    seen = []

    def replace_first_inode(fd, deadline):
        real_lock(fd, deadline)
        seen.append(fd)
        if len(seen) == 1:
            os.unlink(lock_path)
            with open(lock_path, "w"):
                pass

    monkeypatch.setattr(frontmatter, "_lock_exclusive", replace_first_inode)
    with frontmatter.locked(str(tmp_path / "lesson.md"), timeout=1):
        assert len(seen) == 2
        assert os.fstat(seen[-1]).st_ino == os.stat(lock_path).st_ino
    # The OS can reuse descriptor numbers, so verify all are closed after release.
    for fd in seen:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.skipif(frontmatter.fcntl is None, reason="requires POSIX inode identity checks")
def test_inode_churn_cannot_reset_acquisition_timeout(tmp_path, monkeypatch):
    lock_path = str(tmp_path / "lesson.md.lock")
    real_lock = frontmatter._lock_exclusive
    calls = []

    def invalidate(fd, deadline):
        real_lock(fd, deadline)
        calls.append(fd)
        os.unlink(lock_path)

    monkeypatch.setattr(frontmatter, "_lock_exclusive", invalidate)
    with pytest.raises(TimeoutError, match="frontmatter lock"):
        with frontmatter.locked(str(tmp_path / "lesson.md"), timeout=0):
            pass
    assert len(calls) == 1
    with pytest.raises(OSError):
        os.fstat(calls[0])


@pytest.mark.skipif(frontmatter.fcntl is None, reason="requires POSIX file locks")
def test_release_failure_still_closes_descriptor(tmp_path, monkeypatch):
    acquired = []
    real_acquire = frontmatter._acquire_lock_fd

    def record(*args):
        fd = real_acquire(*args)
        acquired.append(fd)
        return fd

    def fail(fd):
        raise OSError(errno.EIO, "failed to unlock")

    monkeypatch.setattr(frontmatter, "_acquire_lock_fd", record)
    monkeypatch.setattr(frontmatter, "_unlock", fail)
    with pytest.raises(OSError, match="failed to unlock"):
        with frontmatter.locked(str(tmp_path / "lesson.md"), timeout=0):
            pass
    assert len(acquired) == 1
    with pytest.raises(OSError):
        os.fstat(acquired[0])
