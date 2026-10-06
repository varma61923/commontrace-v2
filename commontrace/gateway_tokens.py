"""Atomic gateway bearer credentials, immediate revocation and optional expiry.

Legacy plaintext token files remain readable. New writes use one JSON record so
the credential and its lifetime change atomically. File-backed authentication
checks strong file identity on every request and fails closed on read errors.
"""
from __future__ import annotations

import json
import math
import os
import secrets
import stat
import tempfile
import threading
import time

from commontrace import frontmatter, paths

TOKEN_NAME = "gateway.token"


def token_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), TOKEN_NAME)


def _identity(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)


def _read(path: str) -> tuple[dict, tuple]:
    if os.path.islink(path):
        raise OSError("gateway credential file must not be a symlink")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
            raise ValueError("invalid gateway credential file")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ValueError("gateway credential file must belong to this user")
        if info.st_mode & 0o077 and hasattr(os, "fchmod"):
            os.fchmod(fh.fileno(), 0o600)
            info = os.fstat(fh.fileno())
        raw = fh.read(4097).strip()
    if raw.startswith("{"):
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get("version") != 1:
            raise ValueError("unsupported gateway credential record")
    else:
        record = {"version": 1, "token": raw, "expires_at": None}
    token = record.get("token")
    if token is not None and (not isinstance(token, str) or not 24 <= len(token) <= 256
                              or not token.isascii() or any(c.isspace() for c in token)):
        raise ValueError("invalid gateway bearer token")
    expiry = record.get("expires_at")
    if expiry is not None and (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                               or not math.isfinite(expiry)):
        raise ValueError("invalid gateway token expiry")
    return record, _identity(info)


def _write(path: str, record: dict) -> None:
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".gateway-token-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, allow_nan=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        if hasattr(os, "O_DIRECTORY"):
            dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _new_record(ttl: float | None) -> dict:
    if ttl is not None and (not math.isfinite(ttl) or ttl <= 0):
        raise ValueError("token ttl must be positive and finite")
    now = time.time()
    return {"version": 1, "token": secrets.token_urlsafe(32), "created_at": now,
            "expires_at": None if ttl is None else now + ttl}


def rotate_token(root: str, *, ttl: float | None = None) -> str:
    record = _new_record(ttl)
    path = token_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with frontmatter.locked(path):
        # Never follow an existing symlink when replacing credentials.
        if os.path.lexists(path):
            _read(path)
        _write(path, record)
    return record["token"]


def revoke_token(root: str) -> None:
    path = token_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with frontmatter.locked(path):
        if os.path.lexists(path):
            _read(path)
        _write(path, {"version": 1, "token": None, "expires_at": time.time()})


def load_or_create_token(root: str, *, ttl: float | None = None) -> str:
    # Validate even if the file exists so bad configuration never goes unnoticed.
    if ttl is not None and (not math.isfinite(ttl) or ttl <= 0):
        raise ValueError("token ttl must be positive and finite")
    path = token_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with frontmatter.locked(path):
        try:
            record, _ = _read(path)
        except FileNotFoundError:
            record = None
        if record is not None and record.get("token") is None:
            raise ValueError("gateway token was revoked; rotate it explicitly before restarting")
        if record is not None and (record.get("expires_at") is None or record["expires_at"] > time.time()):
            return record["token"]
        record = _new_record(ttl)
        _write(path, record)
        return record["token"]


class FileTokenProvider:
    def __init__(self, root: str) -> None:
        self.path = token_path(root)
        self._lock = threading.Lock()
        self._stamp: tuple | None = None
        self._record: dict = {}

    def __call__(self) -> str | None:
        with self._lock:
            try:
                stamp = _identity(os.stat(self.path, follow_symlinks=False))
                if stamp != self._stamp:
                    self._record, self._stamp = _read(self.path)
                expiry = self._record.get("expires_at")
                if expiry is not None and time.time() >= expiry:
                    return None
                return self._record.get("token")
            except (OSError, ValueError, UnicodeError):
                self._record, self._stamp = {}, None
                return None
