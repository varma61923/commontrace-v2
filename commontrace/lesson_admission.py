"""Review receipts bind lesson admission to content and durable revocation.

AgentPoison (https://arxiv.org/abs/2407.12784) and MemoryGraft
(https://arxiv.org/abs/2512.16962) motivate separating source authenticity and
review from heuristic text screening. A receipt certifies approval of bytes,
not that a claim is true or an instruction safe. Local keys/ledger rely on OS
ownership; this cannot defend against the same OS identity reading the key or
rolling back the entire trusted ledger. Deployment keys can be supplied via
COMMONTRACE_APPROVAL_KEY_FILE, keeping producers separate from the approver.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import stat
import urllib.parse
import uuid
from collections.abc import Mapping
from typing import Any

from commontrace import paths
from commontrace.secrets_provider import env_secret

RECEIPT_FIELD = "approval_receipt"
# Counters and sync bookkeeping do not change the knowledge admitted to agents.
_OPERATIONAL = frozenset({RECEIPT_FIELD, "uses", "last_hit", "hub_trace_id", "hub_pushed_fingerprint"})
_MAX_BYTES = 2 * 1024 * 1024


class AdmissionError(ValueError):
    """Safe admission diagnostic; no secret or untrusted document is included."""


def _json_value(value: Any, depth: int = 0, budget: list[int] | None = None) -> Any:
    budget = budget if budget is not None else [0]
    budget[0] += 1
    if depth > 64 or budget[0] > 100_000:
        raise AdmissionError("lesson admission metadata exceeds structural limits")
    if isinstance(value, str) and len(value) > _MAX_BYTES:
        raise AdmissionError("lesson admission metadata exceeds size limit")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AdmissionError("lesson admission metadata must contain finite numbers")
        return value
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise AdmissionError("lesson admission metadata requires string keys")
        return {k: _json_value(v, depth + 1, budget) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v, depth + 1, budget) for v in value]
    raise AdmissionError("lesson admission metadata must contain JSON values")


def _wire(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(_json_value(value), sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (UnicodeError, RecursionError, TypeError):
        raise AdmissionError("invalid lesson admission metadata") from None
    if len(encoded) > _MAX_BYTES:
        raise AdmissionError("lesson admission metadata exceeds size limit")
    return encoded


def digest_of(fm: Mapping[str, Any], body: str) -> str:
    """Full SHA256 identity for reviewed bytes, routing, privilege and provenance."""
    if isinstance(body, str) and len(body) > _MAX_BYTES:
        raise AdmissionError("lesson body exceeds admission size limit")
    if not isinstance(body, str):
        raise AdmissionError("lesson body must be text")
    governed = {key: value for key, value in fm.items() if key not in _OPERATIONAL}
    return hashlib.sha256(_wire({"frontmatter": governed, "body": body})).hexdigest()


def _ledger_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "lesson_admissions.db")


def _local_key_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), ".approval-key")


def _private_file(path: str, *, create: bytes | None = None, inspect_only: bool = False) -> bytes:
    if create is not None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(create)
                stream.flush()
                os.fsync(stream.fileno())
    if os.path.islink(path):
        raise AdmissionError("lesson admission private files must not be symlinks")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or hasattr(os, "getuid") and info.st_uid != os.getuid()
                    or os.name != "nt" and info.st_mode & 0o077):
                raise AdmissionError("lesson admission private files require private owner-only access")
            if inspect_only:
                return b""
            content = stream.read(4097)
            if len(content) > 4096:
                raise AdmissionError("lesson admission private file exceeds size limit")
            return content
    except OSError:
        raise AdmissionError("lesson admission private file is unavailable") from None


def _key(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) < 32:
        raise AdmissionError("lesson approval keys require at least 32 bytes")
    return encoded


def _keys(root: str, *, create: bool = False) -> tuple[str, dict[str, bytes]]:
    try:
        configured = env_secret("COMMONTRACE_APPROVAL_KEY")
        previous = env_secret("COMMONTRACE_APPROVAL_KEY_PREVIOUS")
        current_id = os.environ.get("COMMONTRACE_APPROVAL_KEY_ID", "deployment")
        previous_id = os.environ.get("COMMONTRACE_APPROVAL_KEY_PREVIOUS_ID", "previous")
        if not current_id or len(current_id) > 128 or not previous_id or len(previous_id) > 128:
            raise AdmissionError("invalid lesson approval key identifier")
        if configured:
            keys = {current_id: _key(configured)}
        else:
            current_id = "local"
            if create:
                from commontrace import frontmatter

                # Serialize first issuers while the reserved key is flushed.
                with frontmatter.locked(_local_key_path(root)):
                    raw = _private_file(_local_key_path(root), create=secrets.token_hex(32).encode())
            else:
                # Verification never creates a lock or private state. A ledger
                # can only reference a key after its creator has flushed it.
                raw = _private_file(_local_key_path(root))
            keys = {current_id: _key(raw.decode().strip())}
        if previous:
            if previous_id in keys:
                raise AdmissionError("lesson approval rotation key identifiers must be distinct")
            keys[previous_id] = _key(previous)
        return current_id, keys
    except (RuntimeError, UnicodeError):
        raise AdmissionError("lesson approval key source is unavailable") from None


def _subject(root: str, path: str) -> str:
    from commontrace.lesson_io import canonical_slug

    base = os.path.realpath(paths.lessons_dir(root))
    candidate = os.path.realpath(path)
    if (os.path.islink(path) or not paths.is_within_directory(root, candidate)
            or os.path.dirname(candidate) != base or not candidate.endswith(".md")):
        raise AdmissionError("lesson admission requires an authoritative local lesson path")
    return canonical_slug(os.path.basename(candidate))


def validate_path(root: str, path: str) -> None:
    """Reject external or aliased lesson sources before opening their contents."""
    _subject(root, path)


def _root_id(root: str) -> str:
    return hashlib.sha256(os.path.realpath(root).encode()).hexdigest()


def _signature(payload: Mapping[str, Any], key: bytes) -> str:
    return hmac.new(key, b"commontrace-lesson-admission-v1\x00" + _wire(payload), hashlib.sha256).hexdigest()


def _open(root: str, *, write: bool = False) -> sqlite3.Connection:
    path = _ledger_path(root)
    if write:
        # Reserve an owned regular file before SQLite can create it permissively.
        _private_file(path, create=b"", inspect_only=True)
    else:
        _private_file(path, inspect_only=True)  # Validate ownership/link/permissions without mutation.
    if write:
        db = sqlite3.connect(path, timeout=5, isolation_level=None)
    else:
        uri = "file:" + urllib.parse.quote(os.path.abspath(path), safe="/") + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
    try:
        if write:
            db.execute("CREATE TABLE IF NOT EXISTS admissions ("
                       "subject TEXT PRIMARY KEY, payload TEXT NOT NULL, signature TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS admission_events ("
                       "sequence INTEGER PRIMARY KEY, subject TEXT NOT NULL, "
                       "payload TEXT NOT NULL, signature TEXT NOT NULL)")
        return db
    except BaseException:
        db.close()
        raise


def _record_state(root: str, path: str, *, action: str, digest: str, actor: str) -> dict[str, Any]:
    subject = _subject(root, path)
    key_id, keys = _keys(root, create=True)
    payload: dict[str, Any] = {
        "version": 1, "root": _root_id(root), "subject": subject, "id": uuid.uuid4().hex,
        "action": action, "digest": digest, "actor": actor,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "key_id": key_id,
    }
    signature = _signature(payload, keys[key_id])
    db = _open(root, write=True)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO admissions VALUES(?,?,?) ON CONFLICT(subject) DO UPDATE SET "
                   "payload=excluded.payload,signature=excluded.signature",
                   (subject, _wire(payload).decode(), signature))
        db.execute("INSERT INTO admission_events(subject,payload,signature) VALUES(?,?,?)",
                   (subject, _wire(payload).decode(), signature))
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    return {"version": 1, "id": payload["id"]}


def _record(root: str, path: str, *, action: str, digest: str, actor: str) -> dict[str, Any]:
    try:
        return _record_state(root, path, action=action, digest=digest, actor=actor)
    except (OSError, sqlite3.Error):
        raise AdmissionError("lesson admission state could not be committed") from None


def issue(root: str, path: str, fm: Mapping[str, Any], body: str, *, actor: str) -> dict[str, Any]:
    """Record an explicit approval before writing its active lesson atomically."""
    if fm.get("status") != "active" or not actor:
        raise AdmissionError("approval receipt requires an active lesson and identified reviewer")
    return _record(root, path, action="approved", digest=digest_of(fm, body), actor=actor)


def revoke(root: str, path: str, *, actor: str) -> None:
    """Durably withdraw an approval; restoring an old active file cannot undo it."""
    if not actor:
        raise AdmissionError("approval revocation requires an identified actor")
    _record(root, path, action="revoked", digest="", actor=actor)


def eligible(root: str, path: str, fm: Mapping[str, Any], body: str, *, as_of: str | None = None) -> bool:
    """Verify current admission. Historical queries never undo current revocation.

    Valid-time selection remains with the retriever; ``as_of`` does not move
    trust back in time. Missing receipts retain legacy behavior unless the
    store explicitly requires integrity-bound approval.
    """
    from commontrace import approval

    del as_of
    try:
        # Unsigned compatibility permits existing documents, never external
        # file reads or symlink aliases of a different lesson identity.
        subject = _subject(root, path)
        strict = approval.load_policy(root).require_integrity
        if fm.get("status", "active") != "active":
            return False
        receipt = fm.get(RECEIPT_FIELD)
        if receipt is None:
            if strict:
                return False
            # Once governed, stripping the receipt cannot restore legacy trust.
            if os.path.exists(_ledger_path(root)):
                db = _open(root)
                try:
                    bound = db.execute("SELECT 1 FROM admissions WHERE subject=?", (subject,)).fetchone()
                finally:
                    db.close()
                return bound is None
            return True
        if (not isinstance(receipt, dict) or set(receipt) != {"version", "id"}
                or type(receipt.get("version")) is not int or receipt["version"] != 1):
            return False
        _, keys = _keys(root)
        db = _open(root)
        try:
            row = db.execute("SELECT payload,signature FROM admissions WHERE subject=?", (subject,)).fetchone()
        finally:
            db.close()
        if row is None:
            return False
        payload = json.loads(row[0])
        key = keys.get(payload.get("key_id"))
        return bool(
            key and hmac.compare_digest(_signature(payload, key), row[1])
            and payload.get("root") == _root_id(root) and payload.get("subject") == subject
            and payload.get("version") == 1 and payload.get("action") == "approved"
            and payload.get("id") == receipt.get("id") and payload.get("digest") == digest_of(fm, body)
        )
    except (AdmissionError, approval.PolicyError, OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        return False


def state_stamp(root: str) -> tuple[tuple[int, int, int, int, int] | str | None, ...]:
    """Fresh identities, including deployment key rotation, for trust caches.

    Key material is never returned. A one-way fingerprint reflects the freshly
    resolved trusted key set, including mounted FILE secrets and key IDs.
    """
    from commontrace.approval import policy_path

    out: list[tuple[int, int, int, int, int] | str | None] = []
    for path in (_ledger_path(root), _local_key_path(root), policy_path(root)):
        try:
            info = os.stat(path)
            out.append((info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size))
        except OSError:
            out.append(None)
    try:
        _, keys = _keys(root)
        out.append(hashlib.sha256(_wire({key_id: hashlib.sha256(key).hexdigest()
                                        for key_id, key in keys.items()})).hexdigest())
    except (AdmissionError, OSError):
        out.append("unavailable")
    return tuple(out)
