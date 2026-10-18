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
from collections.abc import Sequence
from typing import Any, Protocol, cast

from commontrace import _jsonl, memory_authority, paths
from commontrace import frontier_protocol as fp

INDEX = "kv-capsules.jsonl"
MAX_KV_BYTES = 64 * 1024 * 1024


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
    """Process-local reference tier for tests and short-lived workers."""
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.lock = threading.RLock()

    def put(self, key: str, value: bytes) -> None:
        with self.lock:
            self.values[key] = bytes(value)

    def get(self, key: str) -> bytes:
        with self.lock:
            return self.values[key]

    def delete(self, key: str) -> None:
        with self.lock:
            self.values.pop(key, None)


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
        _POOLS[name] = adapter


def register_compressor(name: str, adapter: Compressor) -> None:
    fp.text(name, "KV compressor", 64)
    if name == "identity":
        raise ValueError("identity compressor name is reserved")
    if not callable(getattr(adapter, "compress", None)) or not callable(getattr(adapter, "decompress", None)):
        raise TypeError("KV compressor must implement compress and decompress")
    with _LOCK:
        _COMPRESSORS[name] = adapter


def _model(value: str) -> str:
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError("model fingerprint must be a lowercase SHA-256 digest")
    return value


def _tenant_key(root: str, source_hash: str, model: str) -> str:
    secret = memory_authority._key(root, create=False)
    if secret is None:
        raise PermissionError("capsule key requires an initialized tenant identity key")
    payload = b"commontrace-kv-capsule-v1\0" + bytes.fromhex(source_hash) + bytes.fromhex(model)
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _source(root: str, memory_id: str, context: Sequence[str]) -> tuple[str, str]:
    from commontrace import search_recipes

    admitted = search_recipes.admit(root, [memory_id], context=context)
    if memory_id not in admitted:
        raise PermissionError("KV capsule source is erased, retracted, quarantined or outside scope")
    content_hash = hashlib.sha256(admitted[memory_id].statement.encode("utf-8")).hexdigest()
    return content_hash, str(admitted[memory_id].statement)


def build(root: str, memory_id: str, model_fingerprint: str, kv_bytes: bytes, *,
          promotion_score: float, promotion_threshold: float, tiers: Sequence[str] = ("memory", "disk"),
          compressor: str = "identity", context: Sequence[str] = ()) -> dict[str, Any]:
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
    missing = set(names) - set(_POOLS) - {"disk"}
    if missing:
        raise ValueError("KV pool adapter is not installed: " + ", ".join(sorted(missing)))
    if compressor not in _COMPRESSORS:
        raise ValueError("KV compressor is not installed")
    source_hash, _ = _source(root, memory_id, context)
    key = _tenant_key(root, source_hash, model)
    compressed = _COMPRESSORS[compressor].compress(kv_bytes)
    if not isinstance(compressed, bytes) or len(compressed) > MAX_KV_BYTES:
        raise ValueError("KV compressor returned an invalid or oversized payload")
    record = {"id": key, "memory_id": memory_id, "memory_hash": source_hash, "model_fingerprint": model,
              "kv_digest": hashlib.sha256(compressed).hexdigest(), "kv_bytes": len(compressed), "tiers": names,
              "compressor": compressor, "promotion_score": score, "promotion_threshold": threshold,
              "status": "active"}
    with _jsonl.locked(fp.filename(root, INDEX) + ".write"):
        prior = [row for row in fp.journal(root, INDEX) if row.get("id") == key and row.get("status") == "active"]
        if prior:
            if prior[-1] != record:
                raise ValueError("capsule key already names different bytes or routing metadata")
            return prior[-1]
        completed: list[str] = []
        try:
            for name in names:
                adapter = DiskPool(root, name) if name == "disk" else _POOLS[name]
                adapter.put(key, compressed)
                completed.append(name)
        except Exception:
            for name in reversed(completed):
                try:
                    (DiskPool(root, name) if name == "disk" else _POOLS[name]).delete(key)
                except Exception:
                    pass
            raise RuntimeError("capsule registration failed; completed tiers were rolled back") from None
        signed = fp.append(root, INDEX, record)
    return cast(dict[str, Any], signed["record"])


def get(root: str, memory_id: str, model_fingerprint: str, *, context: Sequence[str] = ()) -> bytes:
    model = _model(model_fingerprint)
    source_hash, _ = _source(root, memory_id, context)
    key = _tenant_key(root, source_hash, model)
    records = [row for row in fp.journal(root, INDEX) if row.get("id") == key]
    if not records or records[-1].get("status") != "active":
        raise KeyError("no active capsule matches this memory and model fingerprint")
    record = records[-1]
    if record["memory_hash"] != source_hash or record["model_fingerprint"] != model:
        raise PermissionError("capsule source or model fingerprint is stale")
    errors = []
    for name in record["tiers"]:
        try:
            adapter = DiskPool(root, name) if name == "disk" else _POOLS[name]
            compressed = adapter.get(key)
            if hashlib.sha256(compressed).hexdigest() != record["kv_digest"]:
                raise ValueError("capsule digest mismatch")
            return _COMPRESSORS[record["compressor"]].decompress(compressed)
        except Exception as exc:
            errors.append(type(exc).__name__)
    raise FileNotFoundError("capsule is unavailable from every registered tier: " + ",".join(errors))


def evict(root: str, memory_id: str, *, reason: str) -> dict[str, Any]:
    if reason not in {"retract", "erase", "quarantine", "supersede"}:
        raise ValueError("capsule eviction reason must be a governed removal event")
    fp.text(memory_id, "memory id")
    matches = [row for row in fp.journal(root, INDEX)
               if row.get("memory_id") == memory_id and row.get("status") == "active"]
    removed, failed, tiers = [], [], set()
    for record in matches:
        key = record["id"]
        record_failed: list[str] = []
        for name in record["tiers"]:
            tiers.add(name)
            try:
                (DiskPool(root, name) if name == "disk" else _POOLS[name]).delete(key)
                removed.append(name)
            except Exception:
                failed.append(name)
                record_failed.append(name)
        if not record_failed:
            fp.append(root, INDEX, {**record, "status": "evicted", "reason": reason})
    from commontrace import change_stream

    event = change_stream.publish(root, reason, memory_id, revision=fp.digest(sorted(removed)),
                                  event_id="kv-" + fp.digest([reason, memory_id, fp.now()])[:32])
    return {"memory_id": memory_id, "reason": reason, "capsule_tiers": sorted(tiers),
            "removed_tiers": sorted(set(removed)), "failed_tiers": sorted(set(failed)),
            "event_sequence": event["sequence"]}


def evict_sources(root: str, identities: Sequence[str]) -> list[dict[str, Any]]:
    """Canonical cascade hook; erase each direct source and report every capsule tier."""
    present = {row.get("memory_id") for row in fp.journal(root, INDEX)}
    receipts = []
    for identity in sorted(set(identities) & present):
        current = [row for row in fp.journal(root, INDEX)
                   if row.get("memory_id") == identity and row.get("status") == "active"]
        if current:
            receipts.append(evict(root, identity, reason="erase"))
    return receipts


def status(root: str, memory_id: str) -> list[dict[str, Any]]:
    return [row for row in fp.journal(root, INDEX) if row.get("memory_id") == memory_id]
