"""Appending a JSONL row must never leave a truncated line behind."""

from __future__ import annotations

import os

import pytest

from unimem import _jsonl


def test_streaming_rows_preserve_reader_policy_and_close_on_early_exit(tmp_path, monkeypatch):
    path = tmp_path / "rows.jsonl"
    path.write_text('\nnot JSON\n[1,2]\n{"id":"a"}\n{"id":"é"}\n', encoding="utf-8")
    assert _jsonl.read_rows(str(path)) == [{"id": "a"}, {"id": "é"}]
    assert list(_jsonl.iter_rows(str(tmp_path / "missing"))) == []
    handles = []

    def tracked_open(*args, **kwargs):
        handle = open(*args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(_jsonl, "open", tracked_open, raising=False)
    rows = _jsonl.iter_rows(str(path))
    assert next(rows) == {"id": "a"}
    assert not handles[0].closed
    rows.close()
    assert handles[0].closed


def test_a_short_write_is_completed_not_truncated(tmp_path, monkeypatch):
    real_write = os.write

    def dribble(fd, data):
        # Write at most 7 bytes per call, as a signal-interrupted or
        # nearly-full filesystem may.
        return real_write(fd, bytes(data[:7]))

    monkeypatch.setattr(_jsonl.os, "write", dribble)
    path = str(tmp_path / "rows.jsonl")
    row = {"id": "a" * 40, "text": "été " * 20}
    _jsonl.append_row(path, row)
    _jsonl.append_row(path, {"id": "b"})
    assert _jsonl.read_rows(path) == [row, {"id": "b"}]


def test_a_write_that_makes_no_progress_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(_jsonl.os, "write", lambda fd, data: 0)
    with pytest.raises(OSError, match="no progress"):
        _jsonl.append_row(str(tmp_path / "rows.jsonl"), {"id": "a"})


def test_a_durable_append_syncs_the_descriptor_it_wrote_with(tmp_path, monkeypatch):
    real_open, real_fsync = os.open, os.fsync
    opened, synced = [], []

    def spy_open(path, flags, *args):
        fd = real_open(path, flags, *args)
        opened.append(fd)
        return fd

    def spy_fsync(fd):
        synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(_jsonl.os, "open", spy_open)
    monkeypatch.setattr(_jsonl.os, "fsync", spy_fsync)
    path = str(tmp_path / "rows.jsonl")
    _jsonl.append_row(path, {"id": "a"}, fsync=True)
    assert len(opened) == 1 and synced == opened
    _jsonl.append_row(path, {"id": "b"})
    assert len(synced) == 1
    assert _jsonl.read_rows(path) == [{"id": "a"}, {"id": "b"}]


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
def test_nofollow_refuses_a_symlinked_journal_before_writing(tmp_path):
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("")
    link = tmp_path / "rows.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError):
        _jsonl.append_row(str(link), {"id": "a"}, nofollow=True)
    assert target.read_text() == ""


def test_a_failed_partial_append_is_rolled_back_so_the_next_row_survives(tmp_path, monkeypatch):
    # Regression: a short write followed by ENOSPC used to leave an
    # unterminated fragment; the next successful append was glued onto it and
    # read_rows silently dropped the merged line.
    path = str(tmp_path / "rows.jsonl")
    _jsonl.append_row(path, {"n": 1})
    real_write = os.write
    calls = []

    def short_then_full(fd, data):
        calls.append(1)
        if len(calls) == 1:
            return real_write(fd, bytes(data[:5]))
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(_jsonl.os, "write", short_then_full)
    with pytest.raises(OSError, match="No space"):
        _jsonl.append_row(path, {"n": 2})
    monkeypatch.setattr(_jsonl.os, "write", real_write)
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == '{"n": 1}\n'
    _jsonl.append_row(path, {"n": 3})
    assert _jsonl.read_rows(path) == [{"n": 1}, {"n": 3}]


def test_a_partial_append_is_kept_when_another_writer_interleaved(tmp_path, monkeypatch):
    # Truncating to our start offset would destroy a concurrent appender's row,
    # so rollback only happens when the file grew by exactly our bytes.
    path = str(tmp_path / "rows.jsonl")
    _jsonl.append_row(path, {"n": 1})
    real_write = os.write
    calls = []

    def interleaved(fd, data):
        calls.append(1)
        if len(calls) == 1:
            written = real_write(fd, bytes(data[:5]))
            with open(path, "a", encoding="utf-8") as other:
                other.write('{"other": true}\n')
            return written
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(_jsonl.os, "write", interleaved)
    with pytest.raises(OSError):
        _jsonl.append_row(path, {"n": 2})
    with open(path, encoding="utf-8") as fh:
        assert '{"other": true}\n' in fh.read()


def test_an_interrupt_mid_append_also_rolls_back(tmp_path, monkeypatch):
    path = str(tmp_path / "rows.jsonl")
    real_write = os.write
    calls = []

    def interrupted(fd, data):
        calls.append(1)
        if len(calls) == 1:
            return real_write(fd, bytes(data[:3]))
        raise KeyboardInterrupt

    monkeypatch.setattr(_jsonl.os, "write", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _jsonl.append_row(path, {"n": 1})
    assert os.path.getsize(path) == 0
