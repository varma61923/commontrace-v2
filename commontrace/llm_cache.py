"""SQLite cache for LLM completions (stdlib only).

Adapts graphiti's ``LLMCache`` (``graphiti_core/llm_client/cache.py``): a
single table, JSON-only values (never pickle), ``INSERT OR REPLACE`` writes,
corrupt rows treated as misses. Key = ``md5(model + prompt)``.

Opt-in like graphiti's (off unless ``COMMONTRACE_LLM_CACHE=1``); path from
``COMMONTRACE_LLM_CACHE_PATH`` or the platform temp dir (a cache, not store
data, so it never lives inside a fleet store).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading

logger = logging.getLogger(__name__)

ENV_ENABLE = "COMMONTRACE_LLM_CACHE"
ENV_PATH = "COMMONTRACE_LLM_CACHE_PATH"

_SCHEMA = "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


def default_path() -> str:
    return os.path.join(tempfile.gettempdir(), "commontrace-llm-cache.db")


def enabled() -> bool:
    return os.environ.get(ENV_ENABLE, "").strip().lower() in ("1", "true", "yes", "on")


def cache_key(model: str, prompt: str) -> str:
    return hashlib.md5(f"{model}\x1f{prompt}".encode("utf-8")).hexdigest()


class LLMCache:
    """Thread-safe SQLite completion cache."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.environ.get(ENV_PATH, "") or default_path()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10.0, check_same_thread=False,
                             isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(_SCHEMA)
        return db

    def get(self, key: str) -> dict | None:
        try:
            with self._lock, self._connect() as db:
                row = db.execute("SELECT value FROM cache WHERE key=?", (key,)).fetchone()
        except OSError:
            return None
        if not row:
            self.misses += 1
            return None
        try:
            value = json.loads(row[0])
        except ValueError:
            self.misses += 1  # corrupt entry reads as a miss, like graphiti's
            return None
        if not isinstance(value, dict):
            self.misses += 1
            return None
        self.hits += 1
        logger.debug("llm cache hit", extra={"key": key[:12]})
        return value

    def set(self, key: str, value: dict) -> None:
        try:
            raw = json.dumps(value)
        except (TypeError, ValueError):
            return  # non-serializable answers are never cached
        try:
            with self._lock, self._connect() as db:
                db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?)", (key, raw))
        except OSError:
            pass  # a cache that cannot write degrades to no cache

    def stats(self) -> dict:
        return {"hits": self.hits, "misses": self.misses, "path": self.path}
