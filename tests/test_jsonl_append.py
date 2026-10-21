"""Appending a JSONL row must never leave a truncated line behind."""
from __future__ import annotations

import os

import pytest

from unimem import _jsonl


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
