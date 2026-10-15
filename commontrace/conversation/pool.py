"""Bounded read-only conversation connections for long-lived service transports."""
from __future__ import annotations

import atexit
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from commontrace.conversation import unicode_index
from commontrace.conversation.store import ConversationError, Store, db_path


class PoolBusy(ConversationError):
    """Read capacity is temporarily unavailable; callers can retry."""


@dataclass
class _Entry:
    key: tuple
    root: str
    store: Store | None = None
    shape: tuple | None = None
    leased: bool = True
    retire: bool = False
    touched: float = 0.0


class ReadPool:
    """Exclusive leases, at most 16 handles and four per exact source path.

    Read requests never migrate databases or hold SQLite write reservations.
    Source replacement and schema/identity changes discard old handles. Every
    recall still acquires its own committed snapshot and enforces its filters.
    """

    def __init__(self, maximum=16, per_path=4, idle_seconds=60.0, wait_seconds=1.0):
        if (isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 256
                or isinstance(per_path, bool) or not isinstance(per_path, int) or not 1 <= per_path <= maximum
                or isinstance(idle_seconds, bool) or not isinstance(idle_seconds, (int, float))
                or isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float))
                or not 0 < idle_seconds <= 3600 or not 0 < wait_seconds <= 30):
            raise ValueError("invalid bounded read-pool limits")
        self.maximum, self.per_path = maximum, per_path
        self.idle_seconds, self.wait_seconds = idle_seconds, wait_seconds
        self._condition = threading.Condition()
        self._entries: list[_Entry] = []
        self._closed = False

    def _remove(self, entry):
        self._entries.remove(entry)
        if entry.store is not None:
            entry.store.close()

    @contextmanager
    def lease(self, root: str, space: str, *, query: str = ""):
        root = os.path.abspath(root)
        path = db_path(root, space)
        try:
            source = os.stat(path)
        except OSError as exc:
            raise ConversationError(f"no conversations stored for space {space!r}") from exc
        if not stat.S_ISREG(source.st_mode):
            raise ConversationError("conversation source is not a regular file")
        key = (path, space, source.st_dev, source.st_ino)
        deadline = time.monotonic() + self.wait_seconds
        entry = None
        while entry is None:
            with self._condition:
                if self._closed:
                    raise PoolBusy("conversation read pool is closed")
                now = time.monotonic()
                for old in list(self._entries):
                    if not old.leased and (old.retire or now - old.touched > self.idle_seconds
                                          or old.key[0] == path and old.key != key):
                        self._remove(old)
                available = [old for old in self._entries if old.key == key and not old.leased and not old.retire]
                if available:
                    entry = max(available, key=lambda old: old.touched)
                    entry.leased = True
                else:
                    same_path = sum(old.key[0] == path for old in self._entries)
                    if len(self._entries) >= self.maximum and same_path < self.per_path:
                        idle = [old for old in self._entries if not old.leased]
                        if idle:
                            self._remove(min(idle, key=lambda old: old.touched))
                    if len(self._entries) < self.maximum and same_path < self.per_path:
                        entry = _Entry(key, root)
                        self._entries.append(entry)  # reserve capacity before opening outside the lock
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise PoolBusy("conversation read capacity is busy; retry shortly")
                        self._condition.wait(remaining)
        try:
            for _ in range(3):
                if entry.store is None:
                    entry.store = Store(root, space, create=False, read_only=True)
                    entry.shape = (entry.store._schema_version, entry.store._units_identity)
                if unicode_index.relevant(query):
                    with entry.store.read_snapshot():
                        ready = unicode_index.installed(entry.store.db) and not unicode_index.dirty(entry.store.db)
                    if not ready:
                        try:
                            # Maintain only derived postings through the normal
                            # writer. Frozen banks retain exact streaming fallback.
                            with Store(root, space, create=False) as writer:
                                writer.prepare_lexical(query)
                        except (OSError, sqlite3.OperationalError):
                            pass
                with entry.store.read_snapshot():
                    shape = (entry.store.db.execute("PRAGMA schema_version").fetchone()[0],
                             entry.store.get_meta("units_identity"))
                    if shape == entry.shape:
                        with self._condition:
                            if self._closed or entry.retire:
                                raise PoolBusy("conversation read pool was closed during acquisition")
                        yield entry.store
                        return
                # Inspection and recall use the same snapshot; a concurrent
                # migration cannot hide new eligibility columns behind old flags.
                entry.store.close()
                entry.store = None
            raise PoolBusy("conversation schema is changing; retry shortly")
        except BaseException:
            entry.retire = True
            raise
        finally:
            with self._condition:
                entry.leased = False
                entry.touched = time.monotonic()
                if self._closed or entry.retire:
                    self._remove(entry)
                self._condition.notify_all()

    def discard_root(self, root):
        with self._condition:
            for entry in list(self._entries):
                if entry.root == os.path.abspath(root):
                    if entry.leased:
                        entry.retire = True
                    else:
                        self._remove(entry)
            self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            for entry in list(self._entries):
                if not entry.leased:
                    self._remove(entry)
            self._condition.notify_all()

    def _after_fork(self):
        # A child must acquire its own connections and locks. Do not invoke
        # parent model-provider cleanup, which may wait on vanished threads.
        self._condition = threading.Condition()
        self._entries = []


READ_POOL = ReadPool()
atexit.register(READ_POOL.close)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=READ_POOL._after_fork)
