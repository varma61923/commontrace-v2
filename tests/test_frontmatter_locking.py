"""Regression tests for frontmatter.locked(): a real cross-process race on
read-modify-write access to the same file.

frontmatter.write() is atomic (NamedTemporaryFile + os.replace()) but that
only guarantees one writer's own write can't be observed half-done -- it
does nothing to stop two concurrent read-modify-writers from each reading
the same starting content, changing a DIFFERENT field, and one silently
overwriting the other's change on write. Verified directly: two OS
processes racing a read-modify-write on the same file (mirroring `lesson
approve` racing `commontrace sync`'s push_active_lessons, which both
read-modify-write the same lesson file) lost one of two concurrent field
changes in 5/5 runs without a lock serializing the critical section.
"""
from __future__ import annotations

import multiprocessing
import time

import pytest

from commontrace import frontmatter


def _locked_read_modify_write(path: str, field: str, value: str, sleep_seconds: float) -> None:
    with frontmatter.locked(path):
        fm, body = frontmatter.read(path)
        time.sleep(sleep_seconds)  # widen the window a race would need
        fm[field] = value
        frontmatter.write(path, fm, body)


def _unlocked_read_modify_write(path: str, field: str, value: str, sleep_seconds: float) -> None:
    fm, body = frontmatter.read(path)
    time.sleep(sleep_seconds)
    fm[field] = value
    frontmatter.write(path, fm, body)


@pytest.mark.skipif(frontmatter.fcntl is None, reason="fcntl-based locking is POSIX-only")
class TestFrontmatterLocking:
    def test_locked_concurrent_writers_both_survive(self, tmp_path):
        """Two processes, each changing a DIFFERENT field under the lock,
        must both see their change survive -- the lock serializes the
        full read-modify-write cycle, so the second writer's read always
        reflects the first writer's already-committed change."""
        path = str(tmp_path / "lesson.md")
        frontmatter.write(path, {"status": "review", "importance": 1}, "body")

        for _ in range(5):  # multiple trials for confidence, not flakiness-hiding
            p1 = multiprocessing.Process(
                target=_locked_read_modify_write, args=(path, "status", "active", 0.05)
            )
            p2 = multiprocessing.Process(
                target=_locked_read_modify_write, args=(path, "importance", "5", 0.05)
            )
            p1.start()
            p2.start()
            p1.join(timeout=10)
            p2.join(timeout=10)
            assert p1.exitcode == 0 and p2.exitcode == 0

            fm, _ = frontmatter.read(path)
            assert fm["status"] == "active", "writer 1's change was lost"
            assert fm["importance"] == "5", "writer 2's change was lost"

            # reset for the next trial
            frontmatter.write(path, {"status": "review", "importance": 1}, "body")

    def test_unlocked_concurrent_writers_can_lose_an_update(self, tmp_path):
        """Sanity check on the test's own premise: WITHOUT the lock, the
        same race demonstrably loses an update -- proving the lock in the
        test above is doing real work, not passing by coincidence.
        Bounded retries because this depends on OS scheduling; if the race
        genuinely stopped reproducing across many trials, the racy helper
        itself would need investigating, not this test."""
        path = str(tmp_path / "lesson.md")

        for attempt in range(20):
            frontmatter.write(path, {"status": "review", "importance": 1}, "body")
            p1 = multiprocessing.Process(
                target=_unlocked_read_modify_write, args=(path, "status", "active", 0.05)
            )
            p2 = multiprocessing.Process(
                target=_unlocked_read_modify_write, args=(path, "importance", "5", 0.05)
            )
            p1.start()
            p2.start()
            p1.join(timeout=10)
            p2.join(timeout=10)

            fm, _ = frontmatter.read(path)
            if fm.get("status") != "active" or fm.get("importance") != "5":
                return  # reproduced a lost update, as expected without locking

        pytest.fail(f"expected the unlocked race to lose an update at least once in {attempt + 1} attempts")
