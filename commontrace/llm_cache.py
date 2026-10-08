"""Private, bounded SQLite/JSON completions with coalesced hot reads.

Opt-in via COMMONTRACE_LLM_CACHE. Failures degrade to provider calls. No pickle,
no shared temporary database, no connections retained across threads or forks.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

from commontrace.runtime_cache import RuntimeCache

logger = logging.getLogger(__name__)
ENV_ENABLE = "COMMONTRACE_LLM_CACHE"
ENV_PATH = "COMMONTRACE_LLM_CACHE_PATH"
MAX_VALUE_BYTES = 1024 * 1024
DEFAULT_MAX_ENTRIES = 10_000
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_TTL_SECONDS = 86_400.0
_SCHEMA = "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
_META_SCHEMA = ("CREATE TABLE IF NOT EXISTS cache_meta (key TEXT PRIMARY KEY, created_at REAL NOT NULL, "
                "size_bytes INTEGER NOT NULL DEFAULT 0)")


def default_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "commontrace", "llm-cache.db")


def enabled() -> bool:
    return os.environ.get(ENV_ENABLE, "").strip().lower() in ("1", "true", "yes", "on")


def cache_key(model: str, prompt: str, *, namespace: str = "") -> str:
    return hashlib.sha256(json.dumps(["v2", namespace, model, prompt], ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


_HOT = RuntimeCache[str](max_entries=256, max_bytes=8 * 1024 * 1024, ttl=300,
                         weigh=lambda k, v: 512 + len(str(k).encode()) + len(v.encode("utf-8")))


class LLMCache:
    """Thread-safe optional cache. Every database connection is explicitly closed."""

    def __init__(self, path: str | None = None, *, ttl: float = DEFAULT_TTL_SECONDS,
                 max_entries: int = DEFAULT_MAX_ENTRIES, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        if not math.isfinite(ttl) or ttl <= 0 or max_entries < 1 or max_bytes < 1:
            raise ValueError("cache ttl and max_entries must be positive and finite")
        self.path = os.path.abspath(path or os.environ.get(ENV_PATH, "") or default_path())
        self.ttl, self.max_entries = ttl, max_entries
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    @contextmanager
    def _connect(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        if os.path.islink(self.path):
            raise OSError("completion cache must not be a symlink")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
                raise OSError("completion cache must be a regular file owned by this user")
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        db = sqlite3.connect(self.path, timeout=1.0, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(_SCHEMA)
            db.execute(_META_SCHEMA)
            if "size_bytes" not in {row[1] for row in db.execute("PRAGMA table_info(cache_meta)")}:
                db.execute("BEGIN IMMEDIATE")
                try:
                    # Another process may have migrated while we awaited the writer lock.
                    if "size_bytes" not in {row[1] for row in db.execute("PRAGMA table_info(cache_meta)")}:
                        db.execute("ALTER TABLE cache_meta ADD COLUMN size_bytes INTEGER NOT NULL DEFAULT 0")
                        db.execute("UPDATE cache_meta SET size_bytes=COALESCE("
                                   "(SELECT length(CAST(value AS BLOB)) FROM cache WHERE key=cache_meta.key),0)")
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
            yield db
        finally:
            db.close()

    def get(self, key: str) -> dict | None:
        value = None
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT c.value FROM cache c JOIN cache_meta m USING(key) "
                    "WHERE c.key=? AND m.created_at>? AND m.created_at<=? "
                    "AND length(CAST(c.value AS BLOB))<=?",
                    (key, time.time() - self.ttl, time.time(), MAX_VALUE_BYTES),
                ).fetchone()
            if row:
                parsed = json.loads(row[0])
                if isinstance(parsed, dict):
                    value = parsed
        except (OSError, sqlite3.Error, ValueError, RecursionError):
            logger.debug("Completion cache read unavailable")
        with self._lock:
            if value is None:
                self.misses += 1
            else:
                self.hits += 1
        return value

    def set(self, key: str, value: dict) -> None:
        self._store(key, value)
        _HOT.invalidate(self._hot_key(key))

    def _store(self, key: str, value: dict) -> None:
        try:
            if not isinstance(value, dict):
                return
            raw = json.dumps(value, allow_nan=False)
            size = len(raw.encode("utf-8"))
            if size > min(MAX_VALUE_BYTES, self.max_bytes):
                return
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    now = time.time()
                    db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?)", (key, raw))
                    db.execute("INSERT OR REPLACE INTO cache_meta VALUES (?, ?, ?)", (key, now, size))
                    db.execute("DELETE FROM cache_meta WHERE created_at<=? OR key IN "
                               "(SELECT key FROM cache_meta ORDER BY created_at DESC, key LIMIT -1 OFFSET ?)",
                               (now - self.ttl, self.max_entries))
                    db.execute("DELETE FROM cache_meta WHERE key IN (SELECT key FROM "
                               "(SELECT key, SUM(size_bytes) OVER (ORDER BY created_at DESC, key) AS retained "
                               "FROM cache_meta) WHERE retained>?)", (self.max_bytes,))
                    db.execute("DELETE FROM cache WHERE key NOT IN (SELECT key FROM cache_meta)")
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
        except (OSError, sqlite3.Error, TypeError, ValueError, RecursionError):
            logger.debug("Completion cache write unavailable")

    def _hot_key(self, key: str) -> tuple:
        return (self.path, key, self.ttl, self.max_entries, self.max_bytes)

    def get_or_compute(self, key: str, compute: Callable[[], dict]) -> dict:
        """Coalesce provider calls; copy hot values and preserve disk expiry."""
        hot_key = self._hot_key(key)

        def load() -> str:
            before = time.time()
            value = self.get(key)
            if value is None or not isinstance(value.get("text"), str):
                value = compute()
                self._store(key, value)
            try:
                with self._connect() as db:
                    row = db.execute("SELECT created_at FROM cache_meta WHERE key=?", (key,)).fetchone()
                created = row[0] if row else before
            except (OSError, sqlite3.Error):
                created = before
            return json.dumps({"expires": min(before + self.ttl, created + self.ttl), "value": value})

        decoded = json.loads(_HOT.get_or_load(hot_key, load))
        if time.time() >= decoded["expires"]:
            _HOT.invalidate(hot_key)
            decoded = json.loads(_HOT.get_or_load(hot_key, load))
        return decoded["value"]

    def stats(self) -> dict:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses, "path": self.path}
