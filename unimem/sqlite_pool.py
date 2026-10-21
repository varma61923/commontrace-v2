"""Bounded idle SQLite ownership; active transactions never share a handle.

Quiesce callers before replacing files or forking. Idle handles are drained in
the parent before fork, so children never use or close inherited SQLite handles.
"""
from __future__ import annotations

import atexit
import os
import sqlite3
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, TypeVar, overload

Key = tuple[str, int, int]
_CursorT = TypeVar("_CursorT", bound=sqlite3.Cursor)


class Connection(sqlite3.Connection):
    """Finalize every live statement before returning or closing a handle."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cursors: weakref.WeakSet[sqlite3.Cursor] = weakref.WeakSet()

    @overload
    def cursor(self, factory: None = None) -> sqlite3.Cursor: ...

    @overload
    def cursor(self, factory: Callable[[sqlite3.Connection], _CursorT]) -> _CursorT: ...

    def cursor(self, factory: Callable[[sqlite3.Connection], sqlite3.Cursor] | None = None) -> sqlite3.Cursor:
        cursor = super().cursor() if factory is None else super().cursor(factory)
        self.cursors.add(cursor)
        return cursor

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        cursor = super().execute(*args, **kwargs)
        self.cursors.add(cursor)
        return cursor

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        cursor = super().executemany(*args, **kwargs)
        self.cursors.add(cursor)
        return cursor

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        cursor = super().executescript(*args, **kwargs)
        self.cursors.add(cursor)
        return cursor

    def close_cursors(self) -> None:
        for cursor in list(self.cursors):
            cursor.close()
        self.cursors.clear()

    def close(self) -> None:
        try:
            self.close_cursors()
        finally:
            super().close()


class ConnectionPool:
    def __init__(self, open_connection: Callable[[str], Connection], *, limit: int = 4) -> None:
        if limit < 1:
            raise ValueError("idle connection limit must be positive")
        self.open_connection = open_connection
        self.limit = limit
        self.idle: OrderedDict[Key, Connection] = OrderedDict()
        self.lock = threading.Lock()
        self.generation = 0
        atexit.register(self.close)
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(before=self._before_fork, after_in_parent=self._after_fork_parent,
                                after_in_child=self._after_fork_child)

    def close(self, path: str | None = None) -> None:
        """Drain idle handles; checked-out handles close when their owner returns."""
        canonical = os.path.realpath(path) if path is not None else None
        with self.lock:
            self.generation += 1
            for key in list(self.idle):
                if canonical is None or key[0] == canonical:
                    self.idle.pop(key).close()

    def _before_fork(self) -> None:
        self.lock.acquire()
        self.generation += 1
        while self.idle:
            self.idle.popitem()[1].close()

    def _after_fork_child(self) -> None:
        self.lock = threading.Lock()

    def _after_fork_parent(self) -> None:
        self.lock.release()

    def borrow(self, path: str) -> tuple[Key, Connection, int]:
        canonical = os.path.realpath(path)
        stamp = os.stat(canonical)
        key = (canonical, stamp.st_dev, stamp.st_ino)
        with self.lock:
            for old in list(self.idle):
                if old[0] == canonical and old != key:
                    self.idle.pop(old).close()
            connection = self.idle.pop(key, None)
            generation = self.generation
        return key, connection if connection is not None else self.open_connection(canonical), generation

    def release(self, key: Key, connection: Connection, generation: int) -> None:
        try:
            stamp = os.stat(key[0])
            unchanged = (stamp.st_dev, stamp.st_ino) == key[1:]
        except OSError:
            unchanged = False
        with self.lock:
            if not unchanged or generation != self.generation:
                connection.close()
                return
            previous = self.idle.pop(key, None)
            if previous is not None:
                previous.close()
            while len(self.idle) >= self.limit:
                self.idle.popitem(last=False)[1].close()
            self.idle[key] = connection
