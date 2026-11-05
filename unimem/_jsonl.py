"""Locked, atomic JSON/JSONL files for the store's mutable side files."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator
from typing import Any, TextIO

from unimem import frontmatter

_held = threading.local()


def _after_fork() -> None:
    # A child inherits thread-local bookkeeping, but not ownership of the
    # parent's critical section. It must acquire its own inter-process lock.
    global _held
    _held = threading.local()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


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


def _replace(path: str, write: Callable[[TextIO], None]) -> None:
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

    def _write(fh: TextIO) -> None:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    _replace(path, _write)


def write_serialized_rows(path: str, rows: tuple[str, ...]) -> str:
    """Atomically write frozen JSON object lines and return their byte checksum.

    Internal canonical writers supply JSON-encoded rows, with escaped embedded
    newlines. The checksum includes each terminating newline and describes the
    exact UTF-8 bytes passed to the atomic writer; it is not an admission proof.
    """
    digest = hashlib.sha256()

    def _write(fh: TextIO) -> None:
        for row in rows:
            line = row + "\n"
            fh.write(line)
            digest.update(line.encode("utf-8"))

    _replace(path, _write)
    return digest.hexdigest()


def write_json(path: str, value: Any) -> None:
    """Atomically replace *path* with *value* as pretty JSON."""

    def _write(fh: TextIO) -> None:
        json.dump(value, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")

    _replace(path, _write)


def discard_unterminated_tail(path: str) -> None:
    """Drop an interrupted final row from an unsigned journal, under its caller's lock.

    Only newline-terminated rows are committed by the incremental reward/signature
    readers. Signed journals must retain their own fail-closed recovery policy.
    """
    try:
        fh = open(path, "r+b")
    except FileNotFoundError:
        return
    with fh:
        end = fh.seek(0, os.SEEK_END)
        if not end:
            return
        fh.seek(end - 1)
        if fh.read(1) == b"\n":
            return
        while end:
            start = max(0, end - 8192)
            fh.seek(start)
            chunk = fh.read(end - start)
            boundary = chunk.rfind(b"\n")
            if boundary >= 0:
                fh.truncate(start + boundary + 1)
                return
            end = start
        fh.truncate(0)


def append_row(path: str, row: dict[str, Any], *, fsync: bool = False, nofollow: bool = False) -> None:
    """Append one JSON object line to *path* in a single write.

    ``fsync`` flushes through the same descriptor that wrote, so durability costs
    no second open. ``nofollow`` refuses a symlink at *path* before writing.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if nofollow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        start = os.fstat(fd).st_size
        done = 0
        try:
            # os.write may write fewer bytes than asked (a signal, a nearly full
            # disk, some network filesystems). A dropped tail leaves a truncated
            # line that read_rows silently skips, so keep writing until done.
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError(f"could not append to {path}: write made no progress")
                done += written
                view = view[written:]
        except BaseException:
            # A failure after a partial write leaves an unterminated fragment;
            # the next successful append would be glued onto it and lost (and a
            # fail-closed signed journal would be bricked). Roll the fragment
            # back, but only when the file grew by exactly our bytes: if another
            # appender interleaved, truncating would destroy its row instead.
            if done:
                with contextlib.suppress(OSError):
                    if os.fstat(fd).st_size == start + done:
                        os.ftruncate(fd, start)
            raise
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)
