from __future__ import annotations

import multiprocessing
import time

import pytest

from commontrace import frontmatter


def _locked_read_modify_write(path: str, field: str, value: str, sleep_seconds: float) -> None:
    with frontmatter.locked(path):
        fm, body = frontmatter.read(path)
        time.sleep(sleep_seconds)
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
        path = str(tmp_path / "lesson.md")
        frontmatter.write(path, {"status": "review", "importance": 1}, "body")

        for _ in range(5):
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

            frontmatter.write(path, {"status": "review", "importance": 1}, "body")

    def test_unlocked_concurrent_writers_can_lose_an_update(self, tmp_path):
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
                return

        pytest.fail(f"expected the unlocked race to lose an update at least once in {attempt + 1} attempts")
