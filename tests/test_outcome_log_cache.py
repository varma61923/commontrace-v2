"""holdout_io.read_outcomes / record_outcome: the incremental read must give exactly the
answers a full re-parse gives, and must never serve a log that was replaced under it."""
import json
import os
import threading

import pytest

from commontrace import holdout_io


def _naive(root):
    """The original behaviour: parse the whole file, last write wins, skip bad lines."""
    path = holdout_io.outcomes_log_path(root)
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("occasion_id"), str) \
                    and row["occasion_id"] and isinstance(row.get("succeeded"), bool):
                out[row["occasion_id"]] = row["succeeded"]
    return out


@pytest.fixture(autouse=True)
def _no_fsync(monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda fd: None)


def test_it_matches_a_full_reparse_as_the_log_grows(tmp_path):
    root = str(tmp_path)
    for i in range(300):
        holdout_io.record_outcome(root, f"o{i}", i % 3 == 0)
        if i % 50 == 0:
            assert holdout_io.read_outcomes(root) == _naive(root)
    assert holdout_io.read_outcomes(root) == _naive(root)
    assert len(holdout_io.read_outcomes(root)) == 300


def test_recording_does_not_reparse_the_whole_log_each_time(tmp_path, monkeypatch):
    root = str(tmp_path)
    calls = []
    real = json.loads
    monkeypatch.setattr(holdout_io.json, "loads", lambda *a, **k: calls.append(1) or real(*a, **k))
    for i in range(400):
        holdout_io.record_outcome(root, f"o{i}", True)
    # Linear: each new line is parsed about once. The old behaviour parsed ~n^2/2 (80,000).
    assert len(calls) < 3 * 400


def test_a_line_appended_by_someone_else_is_seen(tmp_path):
    root = str(tmp_path)
    holdout_io.record_outcome(root, "mine", True)
    assert holdout_io.read_outcomes(root) == {"mine": True}
    with open(holdout_io.outcomes_log_path(root), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"occasion_id": "theirs", "succeeded": False}) + "\n")
    assert holdout_io.read_outcomes(root) == {"mine": True, "theirs": False}
    with pytest.raises(holdout_io.ConflictingOutcome):
        holdout_io.record_outcome(root, "theirs", True)


def test_a_replaced_log_is_never_served_stale(tmp_path):
    root = str(tmp_path)
    holdout_io.record_outcome(root, "old", True)
    assert holdout_io.read_outcomes(root) == {"old": True}
    path = holdout_io.outcomes_log_path(root)
    os.remove(path)
    with open(path, "w", encoding="utf-8") as fh:  # same path, new content, longer than before
        fh.write(json.dumps({"occasion_id": "new-one", "succeeded": False, "at": "x" * 200}) + "\n")
    assert holdout_io.read_outcomes(root) == {"new-one": False}


def test_a_log_rewritten_in_place_with_different_earlier_bytes_is_rebuilt(tmp_path):
    root = str(tmp_path)
    for i in range(5):
        holdout_io.record_outcome(root, f"a{i}", True)
    holdout_io.read_outcomes(root)
    path = holdout_io.outcomes_log_path(root)
    size = os.path.getsize(path)
    body = "".join(json.dumps({"occasion_id": f"b{i}", "succeeded": False}) + "\n" for i in range(5))
    with open(path, "r+", encoding="utf-8") as fh:  # same inode, same-or-longer length, other content
        fh.write(body.ljust(size + 10))
    assert set(holdout_io.read_outcomes(root)) == {f"b{i}" for i in range(5)} == set(_naive(root))


def test_a_truncated_log_is_rebuilt(tmp_path):
    root = str(tmp_path)
    for i in range(20):
        holdout_io.record_outcome(root, f"o{i}", True)
    holdout_io.read_outcomes(root)
    path = holdout_io.outcomes_log_path(root)
    with open(path, "r+", encoding="utf-8") as fh:
        fh.truncate(os.path.getsize(path) // 2)
    assert holdout_io.read_outcomes(root) == _naive(root)


def test_a_torn_final_line_is_read_for_the_call_but_not_remembered(tmp_path):
    root = str(tmp_path)
    holdout_io.record_outcome(root, "whole", True)
    path = holdout_io.outcomes_log_path(root)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"occasion_id": "partial", "succeeded": False}))  # no newline: a write in flight
    assert holdout_io.read_outcomes(root) == {"whole": True, "partial": False} == _naive(root)
    holdout_io.record_outcome(root, "next", True)  # terminates the fragment first
    assert holdout_io.read_outcomes(root) == _naive(root) == {"whole": True, "partial": False, "next": True}


def test_garbage_lines_are_skipped_like_before(tmp_path):
    root = str(tmp_path)
    holdout_io.record_outcome(root, "a", True)
    with open(holdout_io.outcomes_log_path(root), "ab") as fh:
        fh.write(b"not json\n[1,2]\n\xff\xfe\n" + json.dumps({"occasion_id": 5, "succeeded": True}).encode() + b"\n")
    holdout_io.record_outcome(root, "b", False)
    assert holdout_io.read_outcomes(root) == {"a": True, "b": False}


def test_a_duplicate_is_a_no_op_and_a_conflict_still_raises(tmp_path):
    root = str(tmp_path)
    assert holdout_io.record_outcome(root, "x", True) is True
    assert holdout_io.record_outcome(root, "x", True) is False
    with pytest.raises(holdout_io.ConflictingOutcome):
        holdout_io.record_outcome(root, "x", False)
    assert sum(1 for _ in open(holdout_io.outcomes_log_path(root), encoding="utf-8")) == 1


def test_the_returned_dict_is_a_copy(tmp_path):
    root = str(tmp_path)
    holdout_io.record_outcome(root, "x", True)
    holdout_io.read_outcomes(root)["x"] = False
    holdout_io.read_outcomes(root).clear()
    assert holdout_io.read_outcomes(root) == {"x": True}


def test_concurrent_reporters_never_lose_or_duplicate_an_outcome(tmp_path):
    root = str(tmp_path)
    errors = []

    def worker(n):
        try:
            for i in range(60):
                holdout_io.record_outcome(root, f"t{n}-{i}", i % 2 == 0)
                holdout_io.record_outcome(root, f"shared-{i}", True)  # all threads race on these
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    got = holdout_io.read_outcomes(root)
    assert got == _naive(root) and len(got) == 4 * 60 + 60
    with open(holdout_io.outcomes_log_path(root), encoding="utf-8") as fh:
        assert len([line for line in fh if line.strip()]) == len(got)  # no duplicate lines
