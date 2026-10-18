"""Bounded canonical envelopes for the frontier protocols.

Signatures authenticate an operator, not truth. HMAC is a shared-secret MAC,
explicitly labelled, and must not be presented as public-key non-repudiation.
Existing CommonTrace canonical records and hashes are deliberately untouched.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from commontrace import _jsonl, frontmatter, memory_authority, origin, paths

MAX_BYTES = 1_048_576
MAX_ROWS = 100_000
Stamp = tuple[int, int, int, int, int] | None


@dataclass
class _Journal:
    stamp: Stamp
    pins: tuple[Stamp, ...]
    signed: list[dict[str, Any]] = field(default_factory=list)
    tip: str = ""
    weight: int = 0


_CACHE: OrderedDict[str, _Journal] = OrderedDict()
_GUARD = threading.RLock()


def _after_fork() -> None:
    global _GUARD
    _CACHE.clear()
    _GUARD = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _stamp(path: str) -> Stamp:
    try:
        info = os.stat(path)
        return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size
    except FileNotFoundError:
        return None


def _pins(root: str) -> tuple[Stamp, ...]:
    return tuple(_stamp(os.path.join(paths.memory_dir(root), name)) for name in
                 (".origin-key", ".origin-ed25519-key", "origin-signing.json"))


def _remember(path: str, state: _Journal) -> None:
    _CACHE[path] = state
    _CACHE.move_to_end(path)
    while len(_CACHE) > 32 or sum(value.weight for value in _CACHE.values()) > 64 * MAX_BYTES:
        _CACHE.popitem(last=False)


def _state(root: str, name: str) -> _Journal:
    """Caller holds the journal file lock and _GUARD; external edits reverify."""
    path = filename(root, name)
    stamp, pins = _stamp(path), _pins(root)
    cached = _CACHE.get(path)
    if cached is not None and cached.stamp == stamp and cached.pins == pins:
        _CACHE.move_to_end(path)
        return cached
    signed_rows = rows(path)
    previous = ""
    for index, signed in enumerate(signed_rows):
        record = signed.get("record", {})
        if (not verify(root, signed) or record.get("sequence") != index
                or record.get("previous") != previous):
            raise ValueError(f"invalid {name} chain at row {index}")
        previous = digest(signed)
    if _stamp(path) != stamp or _pins(root) != pins:
        raise ValueError("journal or signing identity changed during verification; retry")
    if cached is not None and len(signed_rows) < len(cached.signed):
        link = signed_rows[0].get("record", {}) if signed_rows else {}
        # A rotation restarts the chain with a signed link to the retired chain's
        # length; anything else that shrinks a retained journal is truncation.
        if link.get("kind") != "rotation" or link.get("previous_count", -1) < len(cached.signed):
            raise ValueError("journal was truncated against a retained checkpoint")
    state = _Journal(stamp, pins, signed_rows, previous,
                     frontmatter._retained_size(signed_rows, set()))
    _remember(path, state)
    return state


def wire(value: Any) -> bytes:
    pending = [(value, 0)]
    visited = 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if depth > 64 or visited > 200_000:
            raise ValueError("JSON exceeds depth/node bounds")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("canonical JSON keys must be strings")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth + 1) for child in item)
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                             allow_nan=False).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError("expected finite JSON data") from exc
    if len(encoded) > MAX_BYTES:
        raise ValueError("record exceeds 1 MiB")
    return encoded


def digest(value: Any) -> str:
    return hashlib.sha256(wire(value)).hexdigest()


def text(value: str, label: str = "identifier", maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > maximum or "\x00" in value:
        raise ValueError(f"invalid {label} (1..{maximum} bytes)")
    return value


def number(value: float, low: float, high: float, label: str = "number") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    if not low <= value <= high:
        raise ValueError(f"{label} must be in [{low}, {high}]")
    return float(value)


def count(value: int, low: int, high: int, label: str = "count") -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer in [{low}, {high}]")
    return value


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def moment(value: str) -> str:
    text(value, "timestamp", 64)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def filename(root: str, name: str) -> str:
    if os.path.basename(name) != name or name.startswith("."):
        raise ValueError("invalid journal name")
    path = os.path.join(paths.memory_dir(root), name)
    paths.enforce_boundary(root, path)
    if os.path.islink(path):
        raise ValueError("journal must not be a symlink")
    return path


def rows(path: str) -> list[dict[str, Any]]:
    """Fail closed on corrupt or oversized journals; never skip a partial row."""
    result: list[dict[str, Any]] = []
    try:
        stream = open(path, "rb")
    except FileNotFoundError:
        return result
    with stream:
        total = 0
        while True:
            line = stream.readline(MAX_BYTES + 2)
            if not line:
                break
            total += len(line)
            if total > 64 * MAX_BYTES:
                raise ValueError("journal exceeds 64 MiB; rotate it")
            if len(result) >= MAX_ROWS or len(line) > MAX_BYTES + 1 or not line.endswith(b"\n"):
                raise ValueError("journal is truncated or exceeds its size limit; rotate it")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("journal row must be an object")
            wire(value)
            result.append(value)
    return result


def _json_copy(value: Any) -> Any:
    """Detach bounded JSON containers without a second serialization/parse pass."""
    if isinstance(value, dict):
        return {key: _json_copy(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_copy(child) for child in value]
    return value


def sign(root: str, record: dict[str, Any], *, durable: bool = True) -> dict[str, Any]:
    """Use the store's pinned signing identity, without recursively logging."""
    wire(record)
    config = memory_authority.signing_config(root)
    algorithm = config["algorithm"]
    key = memory_authority._key(root, create=algorithm == "hmac-sha256", algorithm=algorithm, durable=durable)
    if key is None:
        raise PermissionError("store signing key is unavailable")
    principal, authority = memory_authority.bound_writer()
    return origin.bind(_json_copy(record), origin.Principal(principal, "local-store", authority, key, algorithm),
                       _detached=True)


def verify(root: str, signed: dict[str, Any]) -> bool:
    return bool(memory_authority.verify(root, signed))


def append(root: str, name: str, record: dict[str, Any], *, durable: bool = True) -> dict[str, Any]:
    path = filename(root, name)
    with _jsonl.locked(path), _GUARD:
        state = _state(root, name)
        count(len(state.signed), 0, MAX_ROWS - 1)
        body = {**record, "sequence": len(state.signed), "previous": state.tip,
                "recorded_at": now()}
        signed = sign(root, body, durable=durable)
        wire(signed)
        encoded = json.dumps(signed, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > MAX_BYTES:
            raise ValueError("serialized journal record exceeds 1 MiB")
        if (state.stamp[-1] if state.stamp else 0) + len(encoded) + 1 > 64 * MAX_BYTES:
            raise ValueError("journal exceeds 64 MiB; rotate it")
        _jsonl.append_row(path, signed)
        if durable:
            fd = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        state.signed.append(signed)
        state.tip, state.stamp, state.pins = digest(signed), _stamp(path), _pins(root)
        state.weight += frontmatter._retained_size(signed, set()) + 64
        _remember(path, state)
    return copy.deepcopy(signed)


def rotate(root: str, name: str, carry: list[dict[str, Any]], *, durable: bool = True) -> None:
    """Retire a journal's chain and start a fresh one holding only ``carry`` rows.

    For bounded operational journals (store-and-forward queues, edge caches)
    whose live state is far smaller than their history. Row 0 of the new chain
    is a signed ``rotation`` link naming the retired chain's tip and length, so
    the retirement is itself authenticated and other processes' retained
    checkpoints accept it; the replacement is atomic under the journal lock.
    """
    path = filename(root, name)
    with _jsonl.locked(path), _GUARD:
        state = _state(root, name)
        rows_out: list[dict[str, Any]] = []
        previous = ""
        link = {"kind": "rotation", "previous_tip": state.tip, "previous_count": len(state.signed)}
        for index, record in enumerate([link, *carry]):
            signed = sign(root, {**record, "sequence": index, "previous": previous, "recorded_at": now()},
                          durable=durable)
            wire(signed)
            rows_out.append(signed)
            previous = digest(signed)
        _jsonl.write_rows(path, rows_out)
        _CACHE.pop(path, None)


def journal(root: str, name: str) -> list[dict[str, Any]]:
    return events_since(root, name, 0)


def checkpoint(root: str, name: str) -> tuple[int, str]:
    with _GUARD:
        state = _state(root, name)
        return len(state.signed), state.tip


def events_since(root: str, name: str, offset: int) -> list[dict[str, Any]]:
    return event_snapshot(root, name, offset)[2]


def event_snapshot(root: str, name: str, offset: int) -> tuple[int, str, list[dict[str, Any]]]:
    with _GUARD:
        state = _state(root, name)
        count(offset, 0, len(state.signed))
        return len(state.signed), state.tip, [copy.deepcopy(row["record"]) for row in state.signed[offset:]]


def signed_at(root: str, name: str, index: int) -> dict[str, Any]:
    with _GUARD:
        state = _state(root, name)
        count(index, 0, len(state.signed) - 1)
        return copy.deepcopy(state.signed[index])
