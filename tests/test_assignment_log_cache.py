import json
import os

import pytest

from commontrace import gateway, holdout_io


@pytest.fixture
def root(tmp_path):
    r = str(tmp_path / "store")
    holdout_io.configure(r, rate=0.5, salt="cache")
    return r


def _full_read(root):
    path = holdout_io.holdout_log_path(root)
    records, corrupt = [], 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = holdout_io._parse_log_line(line)
            if rec is None:
                corrupt += 1
            else:
                records.append(rec)
    return records, corrupt


def _assign(root, start, n):
    for i in range(start, start + n):
        holdout_io.assign_and_log(root, ["a", "b"], occasion_id=f"o{i}", rate=0.5, salt="cache",
                                  revisions={"a": "r", "b": "r"})


def test_appends_are_read_and_match_a_full_read(root):
    _assign(root, 0, 20)
    assert holdout_io.read_log(root) == _full_read(root)
    _assign(root, 20, 15)
    records, corrupt = holdout_io.read_log(root)
    assert (records, corrupt) == _full_read(root) and len(records) == 70


def test_the_returned_list_is_a_copy(root):
    _assign(root, 0, 3)
    first, _ = holdout_io.read_log(root)
    first.clear()
    assert len(holdout_io.read_log(root)[0]) == 6


def test_a_replaced_or_truncated_log_is_reread_from_scratch(root):
    _assign(root, 0, 10)
    holdout_io.read_log(root)
    path = holdout_io.holdout_log_path(root)
    lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
    open(path, "w", encoding="utf-8").write("".join(lines[:4]))
    assert holdout_io.read_log(root) == _full_read(root) and len(holdout_io.read_log(root)[0]) == 4
    tmp = path + ".new"
    open(tmp, "w", encoding="utf-8").write("".join(lines[4:9]))
    os.replace(tmp, path)
    assert holdout_io.read_log(root) == _full_read(root)


def test_a_same_length_rewrite_of_the_last_line_is_noticed(root):
    _assign(root, 0, 5)
    holdout_io.read_log(root)
    path = holdout_io.holdout_log_path(root)
    text = open(path, encoding="utf-8").read()
    head, last = text[:-1].rsplit("\n", 1)
    new = head + "\n" + last.replace('"revision": "r"', '"revision": "s"') + "\n"
    assert len(new) == len(text) and new != text
    with open(path, "r+", encoding="utf-8") as fh:
        fh.write(new)
    assert holdout_io.read_log(root) == _full_read(root)
    assert holdout_io.read_log(root)[0][-1].revision == "s"


def test_a_torn_last_line_counts_as_corrupt_for_now_and_is_read_once_complete(root):
    _assign(root, 0, 2)
    path = holdout_io.holdout_log_path(root)
    good = json.dumps({"lesson": "a", "occasion_id": "late", "injected": True, "rate": 0.5, "salt": "cache"})
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(good[:20])
    records, corrupt = holdout_io.read_log(root)
    assert corrupt == 1 and len(records) == 4
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(good[20:] + "\n")
    records, corrupt = holdout_io.read_log(root)
    assert corrupt == 0 and records[-1].occasion_id == "late"


def test_corrupt_lines_are_counted_once(root):
    _assign(root, 0, 2)
    with open(holdout_io.holdout_log_path(root), "a", encoding="utf-8") as fh:
        fh.write("not json\n{\"lesson\": 1}\n")
    assert holdout_io.read_log(root)[1] == 2
    _assign(root, 2, 1)
    assert holdout_io.read_log(root) == _full_read(root) and holdout_io.read_log(root)[1] == 2


def _memories(g):
    r = g.handle("GET", "/v1/memories", {"Authorization": "Bearer " + "t" * 40, "Host": "localhost"})
    return json.loads(r.body)


def test_reports_are_reused_while_the_data_is_unchanged_and_refreshed_once_it_changes(root, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(gateway.time, "monotonic", lambda: clock[0])
    _assign(root, 0, 30)
    for i in range(30):
        holdout_io.record_outcome(root, f"o{i}", i % 2 == 0)
    g = gateway.Gateway(root, token="t" * 40)
    calls = []
    real = g._compute_analysis
    monkeypatch.setattr(g, "_compute_analysis", lambda: (calls.append(1), real())[1])
    first = _memories(g)
    clock[0] += gateway.REPORT_MAX_AGE / 2
    assert _memories(g) == first and len(calls) == 1
    _assign(root, 30, 10)
    assert _memories(g) != first and len(calls) == 2
    second = _memories(g)
    _assign(root, 40, 10)
    clock[0] += 1
    assert len(calls) == 2 and _memories(g) == second
    clock[0] += gateway.REPORT_MIN_INTERVAL
    refreshed = _memories(g)
    assert len(calls) == 3 and refreshed != second


def test_a_report_is_refreshed_after_its_maximum_age_or_when_a_trace_or_episode_appears(root, monkeypatch):
    from commontrace import paths
    clock = [1000.0]
    monkeypatch.setattr(gateway.time, "monotonic", lambda: clock[0])
    _assign(root, 0, 4)
    g = gateway.Gateway(root, token="t" * 40)
    calls = []
    real = g._compute_analysis
    monkeypatch.setattr(g, "_compute_analysis", lambda: (calls.append(1), real())[1])
    _memories(g)
    clock[0] += gateway.REPORT_MAX_AGE
    _memories(g)
    assert len(calls) == 2
    for directory in (paths.traces_dir(root), paths.episodes_dir(root)):
        os.makedirs(directory, exist_ok=True)
        clock[0] += gateway.REPORT_MIN_INTERVAL
        _memories(g)
        before = len(calls)
        with open(os.path.join(directory, "x.md"), "w", encoding="utf-8") as fh:
            fh.write("---\n---\n")
        os.utime(directory, ns=(1, 1))
        clock[0] += gateway.REPORT_MIN_INTERVAL
        _memories(g)
        assert len(calls) == before + 1
