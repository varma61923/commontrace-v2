"""Tenant-scoped governed model KV capsules with explicit all-tier eviction.

KV payloads are opaque model data: this module neither evaluates them nor claims
that its adapters implement CacheBlend partial recomputation. Production adapters
must bind tenant isolation and deletion semantics to their own pool guarantees.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from functools import wraps
from typing import Any, ParamSpec, Protocol, TypeVar, cast

from unimem import _jsonl, memory_authority, paths
from unimem import frontier_protocol as fp

INDEX = "kv-capsules.jsonl"
MAX_KV_BYTES = 64 * 1024 * 1024
_P = ParamSpec("_P")
_R = TypeVar("_R")
_LIVE = frozenset({"active", "staging", "eviction_pending"})


def _serialized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        from unimem import incident

        root = cast(str, args[0] if args else kwargs.get("root"))
        # Canonical erasure takes this fence before invoking the capsule cascade.
        with incident.protect(root), _jsonl.locked(fp.filename(root, INDEX) + ".write"):
            return method(*args, **kwargs)

    return guarded


def _latest(root: str) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in fp.journal(root, INDEX) if "id" in row}


class PoolAdapter(Protocol):
    def put(self, key: str, value: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class Compressor(Protocol):
    def compress(self, value: bytes) -> bytes: ...
    def decompress(self, value: bytes) -> bytes: ...


class IdentityCompression:
    def compress(self, value: bytes) -> bytes:
        return value

    def decompress(self, value: bytes) -> bytes:
        return value


class MemoryPool:
    """Process-local LRU tier with explicit retained-byte and entry budgets."""

    def __init__(self, *, max_bytes: int = 256 * 1024 * 1024, max_entries: int = 128) -> None:
        self.max_bytes = fp.count(max_bytes, 1, 64 * 1024 * 1024 * 1024, "pool byte budget")
        self.max_entries = fp.count(max_entries, 1, 100_000, "pool entry budget")
        self.values: OrderedDict[str, bytes] = OrderedDict()
        self._bytes = 0
        self.lock = threading.RLock()

    def put(self, key: str, value: bytes) -> None:
        fp.text(key, "capsule pool key")
        if not isinstance(value, bytes) or len(value) > min(MAX_KV_BYTES, self.max_bytes):
            raise ValueError("capsule pool payload exceeds its byte budget or has an invalid type")
        with self.lock:
            previous = self.values.pop(key, None)
            if previous is not None:
                self._bytes -= len(previous)
            while self.values and (len(self.values) >= self.max_entries or self._bytes + len(value) > self.max_bytes):
                self._bytes -= len(self.values.popitem(last=False)[1])
            self.values[key] = bytes(value)
            self._bytes += len(value)

    def get(self, key: str) -> bytes:
        fp.text(key, "capsule pool key")
        with self.lock:
            value = self.values[key]
            self.values.move_to_end(key)
            return value

    def delete(self, key: str) -> None:
        fp.text(key, "capsule pool key")
        with self.lock:
            value = self.values.pop(key, None)
            if value is not None:
                self._bytes -= len(value)


class DiskPool:
    """Owner-only local disk tier using atomic writes inside the selected store."""

    def __init__(self, root: str, name: str = "disk") -> None:
        self.root = os.path.realpath(root)
        self.directory = os.path.join(paths.memory_dir(root), "kv-capsules", fp.digest(name))

    def _path(self, key: str) -> str:
        if len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key):
            raise ValueError("invalid capsule pool key")
        result = os.path.join(self.directory, key + ".kv")
        paths.enforce_boundary(self.root, result)
        if os.path.islink(result):
            raise ValueError("capsule pool entry cannot be a symlink")
        return result

    def put(self, key: str, value: bytes) -> None:
        if len(value) > MAX_KV_BYTES:
            raise ValueError("capsule exceeds 64 MiB")
        target = self._path(key)
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".capsule-", suffix=".tmp", dir=self.directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def get(self, key: str) -> bytes:
        with open(self._path(key), "rb") as stream:
            value = stream.read(MAX_KV_BYTES + 1)
        if len(value) > MAX_KV_BYTES:
            raise ValueError("capsule exceeds 64 MiB")
        return value

    def delete(self, key: str) -> None:
        try:
            os.unlink(self._path(key))
        except FileNotFoundError:
            pass


_POOLS: dict[str, PoolAdapter] = {"memory": MemoryPool()}
_COMPRESSORS: dict[str, Compressor] = {"identity": IdentityCompression()}
_LOCK = threading.RLock()


def register_pool(name: str, adapter: PoolAdapter) -> None:
    """Install an operator-owned LMCache, Dynamo KVBM, Mooncake or other adapter."""
    fp.text(name, "KV pool adapter", 64)
    if name in {"memory", "disk"}:
        raise ValueError("reference KV pool names are reserved")
    for method in ("put", "get", "delete"):
        if not callable(getattr(adapter, method, None)):
            raise TypeError("KV pool adapter must implement put, get and delete")
    with _LOCK:
        if name in _POOLS and _POOLS[name] is not adapter:
            raise ValueError("KV pool adapter names are immutable; use a new versioned name")
        _POOLS[name] = adapter


def register_compressor(name: str, adapter: Compressor) -> None:
    fp.text(name, "KV compressor", 64)
    if name == "identity":
        raise ValueError("identity compressor name is reserved")
    if not callable(getattr(adapter, "compress", None)) or not callable(getattr(adapter, "decompress", None)):
        raise TypeError("KV compressor must implement compress and decompress")
    with _LOCK:
        if name in _COMPRESSORS and _COMPRESSORS[name] is not adapter:
            raise ValueError("KV compressor names are immutable; use a new versioned name")
        _COMPRESSORS[name] = adapter


def _model(value: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError("model fingerprint must be a lowercase SHA-256 digest")
    return value


def _tenant_key(root: str, source_hash: str, model: str, *, memory_id: str, revision: str) -> str:
    secret = memory_authority._key(root, create=False)
    if secret is None:
        raise PermissionError("capsule key requires an initialized tenant identity key")
    payload = b"unimem-kv-capsule-v2\0" + fp.wire([memory_id, revision, source_hash, model])
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _source(root: str, memory_id: str, context: Sequence[str]) -> tuple[str, str]:
    from unimem import search_recipes

    fp.text(memory_id, "memory id")
    if (
        not isinstance(context, Sequence)
        or isinstance(context, (str, bytes, bytearray))
        or any(not isinstance(label, str) for label in context)
    ):
        raise ValueError("capsule context must be a sequence of scope labels")
    admitted = search_recipes.admit(root, [memory_id], context=context)
    if memory_id not in admitted:
        raise PermissionError("KV capsule source is erased, retracted, quarantined or outside scope")
    content_hash = hashlib.sha256(admitted[memory_id].statement.encode("utf-8")).hexdigest()
    return content_hash, str(admitted[memory_id].revision)


@_serialized
def build(
    root: str,
    memory_id: str,
    model_fingerprint: str,
    kv_bytes: bytes,
    *,
    promotion_score: float,
    promotion_threshold: float,
    tiers: Sequence[str] = ("memory", "disk"),
    compressor: str = "identity",
    context: Sequence[str] = (),
) -> dict[str, Any]:
    """Compile one admitted, over-threshold memory into tenant/model-bound opaque KV."""
    model = _model(model_fingerprint)
    score = fp.number(promotion_score, 0, 1, "promotion score")
    threshold = fp.number(promotion_threshold, 0, 1, "promotion threshold")
    if score < threshold:
        raise PermissionError("memory has not reached the configured capsule promotion threshold")
    if not isinstance(kv_bytes, bytes) or not kv_bytes or len(kv_bytes) > MAX_KV_BYTES:
        raise ValueError("KV payload must be nonempty bytes no larger than 64 MiB")
    names = sorted(set(fp.text(name, "tier", 64) for name in tiers))
    fp.count(len(names), 1, 16)
    with _LOCK:
        missing = set(names) - set(_POOLS) - {"disk"}
        if missing:
            raise ValueError("KV pool adapter is not installed: " + ", ".join(sorted(missing)))
        if compressor not in _COMPRESSORS:
            raise ValueError("KV compressor is not installed")
        codec = _COMPRESSORS[compressor]
        adapters = {name: DiskPool(root, name) if name == "disk" else _POOLS[name] for name in names}
    source_hash, revision = _source(root, memory_id, context)
    key = _tenant_key(root, source_hash, model, memory_id=memory_id, revision=revision)
    compressed = codec.compress(kv_bytes)
    if not isinstance(compressed, bytes) or len(compressed) > MAX_KV_BYTES:
        raise ValueError("KV compressor returned an invalid or oversized payload")
    record = {
        "id": key,
        "memory_id": memory_id,
        "memory_hash": source_hash,
        "source_revision": revision,
        "key_version": 2,
        "model_fingerprint": model,
        "kv_digest": hashlib.sha256(compressed).hexdigest(),
        "kv_bytes": len(compressed),
        "tiers": names,
        "compressor": compressor,
        "promotion_score": score,
        "promotion_threshold": threshold,
        "status": "active",
    }
    prior = _latest(root).get(key)
    if prior and prior["status"] == "active":
        if any(prior.get(field) != value for field, value in record.items()):
            raise ValueError("capsule key already names different bytes or routing metadata")
        return prior
    if prior and prior["status"] in {"staging", "eviction_pending"}:
        raise ValueError("capsule cleanup is pending; evict it before rebuilding")
    # Persist ownership before any adapter can write, including writes whose reply
    # is lost. Interrupted registration remains discoverable and never readable.
    fp.append(root, INDEX, {**record, "status": "staging"})
    try:
        for adapter in adapters.values():
            adapter.put(key, compressed)
        if _source(root, memory_id, context) != (source_hash, revision):
            raise ValueError("capsule source changed during registration")
    except Exception as exc:
        try:
            _, failed = _delete_record(root, record, "registration_failed", adapters=adapters)
        except Exception:
            raise RuntimeError("capsule registration failed; durable cleanup is pending") from exc
        detail = "cleanup is pending: " + ",".join(failed) if failed else "all tiers were cleaned"
        raise RuntimeError("capsule registration failed; " + detail) from exc
    # If this append fails, retain the staged ownership and bytes. A retry can
    # inspect an uncertain commit without deleting a possibly committed capsule.
    signed = fp.append(root, INDEX, record)
    return cast(dict[str, Any], signed["record"])


@_serialized
def get(root: str, memory_id: str, model_fingerprint: str, *, context: Sequence[str] = ()) -> bytes:
    model = _model(model_fingerprint)
    source_hash, revision = _source(root, memory_id, context)
    key = _tenant_key(root, source_hash, model, memory_id=memory_id, revision=revision)
    record = _latest(root).get(key)
    if not record or record.get("status") != "active":
        raise KeyError("no active capsule matches this memory and model fingerprint")
    if (
        record["memory_hash"] != source_hash
        or record["model_fingerprint"] != model
        or record.get("memory_id") != memory_id
        or record.get("source_revision") != revision
        or record.get("key_version") != 2
    ):
        raise PermissionError("capsule source or model fingerprint is stale")
    errors = []
    for name in record["tiers"]:
        try:
            adapter = DiskPool(root, name) if name == "disk" else _POOLS[name]
            compressed = adapter.get(key)
            if not isinstance(compressed, bytes) or len(compressed) > MAX_KV_BYTES:
                raise ValueError("capsule pool returned an invalid or oversized payload")
            if hashlib.sha256(compressed).hexdigest() != record["kv_digest"]:
                raise ValueError("capsule digest mismatch")
            value = _COMPRESSORS[record["compressor"]].decompress(compressed)
            if not isinstance(value, bytes) or not value or len(value) > MAX_KV_BYTES:
                raise ValueError("capsule decompressor returned an invalid or oversized payload")
        except Exception as exc:
            errors.append(type(exc).__name__)
            continue
        if _source(root, memory_id, context) != (source_hash, revision):
            raise PermissionError("capsule source changed during retrieval")
        return value
    raise FileNotFoundError("capsule is unavailable from every registered tier: " + ",".join(errors))


def _delete_record(
    root: str, record: dict[str, Any], reason: str, *, adapters: dict[str, PoolAdapter] | None = None
) -> tuple[list[str], list[str]]:
    # Revoke reads durably before deleting the first copy. Partial failure must
    # never make a surviving tier an authorized fallback.
    if record["status"] != "eviction_pending":
        fp.append(root, INDEX, {**record, "status": "eviction_pending", "reason": reason})
    removed, failed = [], []
    for name in record["tiers"]:
        try:
            adapter = (
                adapters[name] if adapters is not None else DiskPool(root, name) if name == "disk" else _POOLS[name]
            )
            adapter.delete(record["id"])
            removed.append(name)
        except Exception:
            failed.append(name)
    if not failed:
        fp.append(root, INDEX, {**record, "status": "evicted", "reason": reason})
    return removed, failed


def _evict_records(root: str, memory_id: str, records: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    removed, failed, tiers = [], [], set()
    for record in records:
        tiers.update(record["tiers"])
        done, pending = _delete_record(root, record, reason)
        removed.extend(done)
        failed.extend(pending)
    from unimem import change_stream

    event = change_stream.publish(
        root,
        reason,
        memory_id,
        revision=fp.digest(sorted(removed)),
        event_id="kv-" + fp.digest([reason, memory_id, fp.now()])[:32],
    )
    return {
        "memory_id": memory_id,
        "reason": reason,
        "capsule_tiers": sorted(tiers),
        "removed_tiers": sorted(set(removed)),
        "failed_tiers": sorted(set(failed)),
        "event_sequence": event["sequence"],
    }


@_serialized
def evict(root: str, memory_id: str, *, reason: str) -> dict[str, Any]:
    if reason not in {"retract", "erase", "quarantine", "supersede"}:
        raise ValueError("capsule eviction reason must be a governed removal event")
    fp.text(memory_id, "memory id")
    matches = [
        row for row in _latest(root).values() if row.get("memory_id") == memory_id and row.get("status") in _LIVE
    ]
    return _evict_records(root, memory_id, matches, reason)


@_serialized
def evict_sources(root: str, identities: Sequence[str]) -> list[dict[str, Any]]:
    """Canonical cascade hook; erase each direct source and report every capsule tier."""
    wanted = set(identities)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in _latest(root).values():
        if row.get("memory_id") in wanted and row.get("status") in _LIVE:
            grouped.setdefault(row["memory_id"], []).append(row)
    return [_evict_records(root, identity, grouped[identity], "erase") for identity in sorted(grouped)]


def status(root: str, memory_id: str) -> list[dict[str, Any]]:
    return [row for row in fp.journal(root, INDEX) if row.get("memory_id") == memory_id]
