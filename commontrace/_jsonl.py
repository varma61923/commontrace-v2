"""Locked, atomic JSON/JSONL files for the store's mutable side files."""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from collections.abc import Iterable, Iterator
from typing import Any

from commontrace import frontmatter

_held = threading.local()


@contextlib.contextmanager
def locked(path: str) -> Iterator[None]:
    """Exclusive inter-process lock for *path*, reentrant within a thread."""
    key = os.path.abspath(path)
    held: set[str] = getattr(_held, "paths", None) or set()
    _held.paths = held
    if key in held:
        yield
        return
    held.add(key)
    try:
        with frontmatter.locked(key):
            yield
    finally:
        held.discard(key)


def read_rows(path: str) -> list[dict[str, Any]]:
    """Every JSON object line in *path*; unreadable lines are skipped."""
    rows: list[dict[str, Any]] = []
    try:
        fh = open(path, encoding="utf-8")
    except FileNotFoundError:
        return rows
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _replace(path: str, write) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            write(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_rows(path: str, rows: Iterable[dict[str, Any]]) -> None:
    """Atomically replace *path* with one JSON object per line."""
    def _write(fh) -> None:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    _replace(path, _write)


def write_json(path: str, value: Any) -> None:
    """Atomically replace *path* with *value* as pretty JSON."""
    def _write(fh) -> None:
        json.dump(value, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")

    _replace(path, _write)


def append_row(path: str, row: dict[str, Any]) -> None:
    """Append one JSON object line to *path* in a single write."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
