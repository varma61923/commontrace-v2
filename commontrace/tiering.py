"""UniMem managed namespace payload tiers, residency and embedding generations.

Canonical source authority and audit metadata stay local. Only managed namespace
payloads move; mixed-tenant legacy SQLite files are never blindly archived.
Region tags describe operator-managed placement, not physical geolocation proof.
Native filtering follows the candidate-narrowing approach of ACORN
(arXiv:2403.04871); exact SQLite equivalence does not imply ANN recall equality.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TextIO

from commontrace import _jsonl, paths, safety
from commontrace import frontier_protocol as fp
from commontrace import nextwave_store as store


class ColdBackend(Protocol):
    def put(self, key: str, value: bytes, region: str) -> None: ...
    def get(self, key: str, region: str) -> bytes: ...
    def delete(self, key: str, region: str) -> None: ...


def _sync_parents(filename: str, root: str) -> None:
    if os.name != "posix":
        return
    current, boundary = Path(filename).parent, Path(os.path.realpath(root))
    for _ in range(16):
        paths.enforce_boundary(root, str(current))
        descriptor = os.open(current, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if current == boundary:
            return
        current = current.parent
    raise ValueError("UniMem tier directory depth exceeds its durability bound")


def _record(root: str, kind: str, identity: str, value: dict[str, Any]) -> None:
    store.event(root, kind, identity, value)
    _sync_parents(fp.filename(root, store.journal_name(kind)), root)


def _write(filename: str, raw: bytes, root: str) -> None:
    def write(stream: TextIO) -> None:
        stream.write(raw.decode())

    _jsonl._replace(filename, write)
    _sync_parents(filename, root)


def _object_key(key: str) -> None:
    if re.fullmatch(r"tier-[0-9a-f]{32}/[0-9a-f]{64}\.json", key) is None:
        raise ValueError("UniMem invalid cold payload key")


class LocalCold:
    def __init__(self, root: str):
        self.root = root
        self.directory = os.path.join(paths.memory_dir(root), "tier-cold")

    def _path(self, key: str, region: str) -> str:
        _object_key(key)
        filename = os.path.join(self.directory, fp.digest(region), key)
        paths.enforce_boundary(self.root, filename)
        if os.path.islink(filename):
            raise ValueError("UniMem cold payload cannot be a symlink")
        return filename

    def put(self, key: str, value: bytes, region: str) -> None:
        if len(value) > store.MAX_BLOB + 1:
            raise ValueError("UniMem cold payload is oversized")
        filename = self._path(key, region)
        os.makedirs(os.path.dirname(filename), mode=0o700, exist_ok=True)
        _write(filename, value, self.root)

    def get(self, key: str, region: str) -> bytes:
        with open(self._path(key, region), "rb") as stream:
            value = stream.read(store.MAX_BLOB + 2)
        if len(value) > store.MAX_BLOB + 1:
            raise ValueError("UniMem cold payload exceeds its bound")
        return value

    def delete(self, key: str, region: str) -> None:
        try:
            os.unlink(self._path(key, region))
        except FileNotFoundError:
            pass


_BACKENDS: dict[str, ColdBackend] = {}
_ACCESS: ContextVar[tuple[str, str, str] | None] = ContextVar("unimem_tier_residency", default=None)


def register_backend(name: str, backend: ColdBackend) -> None:
    """Trusted operator process configuration; no request-controlled imports."""
    store.operator()
    _BACKENDS[fp.text(name)] = backend


def configure(
    root: str,
    namespace: str,
    *,
    region: str,
    allowed_regions: Sequence[str],
    cold_region: str,
    scopes: Sequence[str] = (),
    backend: str = "local",
) -> dict[str, Any]:
    store.operator()
    fp.text(namespace)
    allowed = sorted(set(fp.text(r, "region", 128) for r in allowed_regions))
    fp.count(len(allowed), 1, 32)
    if region not in allowed or cold_region not in allowed:
        raise PermissionError("UniMem tier placement violates namespace residency")
    if backend != "local" and backend not in _BACKENDS:
        raise ValueError("UniMem cold backend is not configured in this operator process")
    from commontrace import memory_authority

    key = memory_authority._key(root, create=True)
    if key is None:
        raise PermissionError("UniMem tier configuration requires the tenant identity key")
    tenant = fp.digest({"domain": "unimem.tier.tenant.v1", "key_commitment": fp.digest(key.hex())})
    value = {
        "namespace": namespace,
        "scopes": sorted(set(scopes)),
        "kind": "tier-" + fp.digest([tenant, namespace])[:32],
        "region": region,
        "cold_region": cold_region,
        "allowed_regions": allowed,
        "backend": backend,
    }
    store.put(root, "tier-namespaces", namespace, value, scopes=scopes, immutable=True)
    return value


def _namespace(root: str, namespace: str, region: str, context: Sequence[str]) -> dict[str, Any]:
    value = store.get(root, "tier-namespaces", namespace, context=context)
    if region not in value["allowed_regions"]:
        raise PermissionError("UniMem namespace read/write violates residency")
    return value


def _backend(root: str, value: dict[str, Any]) -> ColdBackend:
    if value["backend"] == "local":
        return LocalCold(root)
    if value["backend"] not in _BACKENDS:
        raise ValueError("UniMem configured cold backend is not installed")
    return _BACKENDS[value["backend"]]


def _state(root: str, namespace: str) -> dict[str, Any]:
    return store.latest(root, "tier-state").get(namespace, {"state": "hot", "payloads": []})


def status(root: str, namespace: str, *, region: str, context: Sequence[str] = ()) -> dict[str, Any]:
    value = _namespace(root, namespace, region, context)
    return {
        **value,
        **{k: v for k, v in _state(root, namespace).items() if k in ("state", "payloads", "at")},
        "boundary": "managed namespace payloads; canonical source/audit metadata remains local",
    }


def put(
    root: str,
    namespace: str,
    identity: str,
    value: dict[str, Any],
    *,
    region: str,
    sources: Sequence[str],
    context: Sequence[str] = (),
) -> dict[str, Any]:
    config = _namespace(root, namespace, region, context)
    admitted = store.admit(root, sources, context=context)
    if set(admitted) != set(sources) or not admitted:
        raise PermissionError("UniMem tier payload requires live admitted sources")
    labels = sorted(set(config["scopes"]) | {s for v in admitted.values() for s in v.get("scopes", [])})
    with _jsonl.locked(fp.filename(root, store.journal_name(config["kind"]))):
        with access(root, namespace, region=region, context=context):
            result = store.put(root, config["kind"], identity, value, scopes=labels, sources=sources, immutable=True)
            _sync_parents(store._blob(root, config["kind"], result["digest"]), root)
            _sync_parents(fp.filename(root, store.journal_name(config["kind"])), root)
            return result


def get(root: str, namespace: str, identity: str, *, region: str, context: Sequence[str] = ()) -> dict[str, Any]:
    config = _namespace(root, namespace, region, context)
    with access(root, namespace, region=region, context=context):
        value = store.get(root, config["kind"], identity, context=context)
    store.event(root, "tier-access", namespace, {"at": fp.now()})
    return value


def hibernate(
    root: str, namespace: str, *, region: str, context: Sequence[str] = (), idle_seconds: int = 0
) -> dict[str, Any]:
    store.operator()
    config = _namespace(root, namespace, region, context)
    fp.count(idle_seconds, 0, 365 * 86400)
    last = max(
        store.latest(root, "tier-access").get(namespace, {}).get("at", ""),
        _state(root, namespace).get("at", ""),
        store.latest(root, "tier-namespaces")[namespace]["recorded_at"],
    )
    if (
        idle_seconds
        and (datetime.fromisoformat(fp.now()) - datetime.fromisoformat(last)).total_seconds() < idle_seconds
    ):
        return {"namespace": namespace, "state": _state(root, namespace)["state"], "reason": "not idle"}
    if config["cold_region"] not in config["allowed_regions"]:
        raise PermissionError("UniMem cold migration violates residency")
    kind = config["kind"]
    backend = _backend(root, config)
    with _jsonl.locked(fp.filename(root, store.journal_name(kind))):
        records = store.latest(root, kind)
        sources = {
            source for row in records.values() if row.get("status") == "active" for source in row.get("sources", [])
        }
        from commontrace import lineage

        protected_sources = set(sources)
        for source in sources:
            protected_sources.update(lineage.ancestors(root, source))
        safety.assert_mutable(root, list(protected_sources))
        if set(store.admit(root, list(sources), context=context)) != sources:
            raise PermissionError("UniMem hibernation sources are unavailable or outside scope")
        if _state(root, namespace)["state"] == "cold":
            return status(root, namespace, region=region, context=context)
        directory = Path(paths.memory_dir(root)) / "unimem" / kind
        files = sorted(directory.glob("*.json"))
        fp.count(len(files), 0, 8192)
        payloads = []
        # Copy+verify all objects before withdrawing any hot file. An interrupted
        # copy leaves the complete hot tier authoritative and can be retried.
        for filename in files:
            paths.enforce_boundary(root, str(filename))
            if filename.is_symlink():
                raise ValueError("UniMem hibernation cannot follow symlinks")
            with filename.open("rb") as stream:
                raw = stream.read(store.MAX_BLOB + 2)
            if len(raw) > store.MAX_BLOB + 1 or fp.digest(json.loads(raw)) != filename.stem:
                raise ValueError("UniMem hot payload content hash is invalid")
            key = kind + "/" + filename.name
            backend.put(key, raw, config["cold_region"])
            if backend.get(key, config["cold_region"]) != raw:
                raise ValueError("UniMem cold copy did not round-trip")
            payloads.append(filename.stem)
        _record(root, "tier-state", namespace, {"state": "cold", "payloads": payloads, "at": fp.now()})
        for filename in files:
            filename.unlink(missing_ok=True)
        if directory.exists():
            _sync_parents(str(directory / ".sync"), root)
    return status(root, namespace, region=region, context=context)


def warm(root: str, namespace: str, *, region: str, context: Sequence[str] = ()) -> dict[str, Any]:
    config = _namespace(root, namespace, region, context)
    kind = config["kind"]
    with _jsonl.locked(fp.filename(root, store.journal_name(kind))):
        state = _state(root, namespace)
        if state["state"] != "cold" and not state.get("cleanup_pending"):
            return {"namespace": namespace, "state": "hot", "already_warm": True}
        backend = _backend(root, config)
        if state["state"] == "hot":
            for digest in state["payloads"]:
                backend.delete(kind + "/" + digest + ".json", config["cold_region"])
            _record(root, "tier-state", namespace, {"state": "hot", "payloads": [], "at": fp.now()})
            return {"namespace": namespace, "state": "hot", "already_warm": True}
        records = store.latest(root, kind)
        sources = {s for row in records.values() if row.get("status") == "active" for s in row.get("sources", [])}
        if set(store.admit(root, list(sources), context=context)) != sources:
            raise PermissionError("UniMem pre-warm sources are unavailable or outside scope")
        directory = Path(paths.memory_dir(root)) / "unimem" / kind
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        active_digests = {row["digest"] for row in store.latest(root, kind).values() if row.get("status") == "active"}
        for digest in state["payloads"]:
            if digest not in active_digests:
                continue
            raw = backend.get(kind + "/" + digest + ".json", config["cold_region"])
            if fp.digest(json.loads(raw)) != digest:
                raise ValueError("UniMem cold payload digest mismatch")
            filename = directory / (digest + ".json")
            paths.enforce_boundary(root, str(filename))
            _write(str(filename), raw, root)
        _record(
            root,
            "tier-state",
            namespace,
            {"state": "hot", "payloads": state["payloads"], "cleanup_pending": True, "at": fp.now()},
        )
        # Keep no hidden cold duplicate after warming.
        for digest in state["payloads"]:
            backend.delete(kind + "/" + digest + ".json", config["cold_region"])
        _record(root, "tier-state", namespace, {"state": "hot", "payloads": [], "at": fp.now()})
    return {"namespace": namespace, "state": "hot", "already_warm": False}


def prewarm(root: str, namespace: str, *, region: str, trigger: str, context: Sequence[str] = ()) -> dict[str, Any]:
    if trigger not in {"session-start", "robot-boot", "task-assignment"}:
        raise ValueError("UniMem unknown namespace pre-warm trigger")
    result = warm(root, namespace, region=region, context=context)
    store.event(
        root,
        "tier-prewarm",
        fp.digest([namespace, trigger, fp.now()]),
        {"namespace": namespace, "trigger": trigger, "at": fp.now()},
    )
    return result


def purge_cold(root: str, kind: str, digests: Sequence[str]) -> list[str]:
    """Canonical erasure hook: deletion may cross residency, serving may not."""
    removed = []
    for metadata in store.latest(root, "tier-namespaces").values():
        if metadata.get("status") != "active":
            continue
        # Canonical deletion is already authorized by its caller. Read only
        # verified routing metadata internally; do not elevate serving scope.
        filename = store._blob(root, "tier-namespaces", metadata["digest"])
        with open(filename, "rb") as stream:
            raw = stream.read(store.MAX_BLOB + 2)
        if len(raw) > store.MAX_BLOB + 1:
            raise ValueError("UniMem tier metadata exceeds the bound")
        config = json.loads(raw)
        if fp.digest(config) != metadata["digest"]:
            raise ValueError("UniMem tier routing metadata is invalid")
        if config["kind"] != kind:
            continue
        backend = _backend(root, config)
        for digest in digests:
            backend.delete(kind + "/" + digest + ".json", config["cold_region"])
        removed.append(config["backend"] + ":" + config["cold_region"])
    return removed


def _vector(value: Sequence[float]) -> list[float]:
    fp.count(len(value), 1, 8192)
    result = [fp.number(x, -1e6, 1e6) for x in value]
    if not any(result):
        raise ValueError("UniMem embedding cannot be the zero vector")
    return result


def _top(vectors: dict[str, list[float]], query: Sequence[float], limit: int) -> list[str]:
    q = _vector(query)
    qn = math.sqrt(sum(x * x for x in q))
    if any(len(v) != len(q) for v in vectors.values()):
        raise ValueError("UniMem embedding dimension mismatch")
    scored = [
        (sum(a * b for a, b in zip(q, v)) / (qn * math.sqrt(sum(x * x for x in v))), i) for i, v in vectors.items()
    ]
    return [i for _, i in sorted(scored, key=lambda pair: (-pair[0], pair[1]))[:limit]]


def reembed(
    root: str,
    namespace: str,
    model: str,
    vectors: dict[str, Sequence[float]],
    *,
    region: str,
    evaluation: Sequence[dict[str, Any]],
    context: Sequence[str] = (),
    minimum_delta: float = 0,
    top_k: int = 5,
) -> dict[str, Any]:
    store.operator()
    fp.text(model, "model fingerprint", 128)
    fp.count(len(vectors), 1, 128)
    fp.count(len(evaluation), 1, 128)
    fp.count(top_k, 1, 100)
    fp.number(minimum_delta, -1, 1)
    config = _namespace(root, namespace, region, context)
    values = {fp.text(i): _vector(v) for i, v in vectors.items()}
    if len({len(v) for v in values.values()}) != 1:
        raise ValueError("UniMem mixed embedding dimensions")
    if set(store.admit(root, list(values), context=context)) != set(values):
        raise PermissionError("UniMem embedding generation contains denied sources")
    with _jsonl.locked(fp.filename(root, "unimem-tier-generations.jsonl") + ".switch"):
        previous = store.latest(root, "tier-generations").get(namespace, {})
        old = get(root, namespace, previous["generation"], region=region, context=context) if previous else None
        if old and set(old["vectors"]) != set(values):
            raise ValueError("UniMem recall delta requires the same held-out candidate universe")
        measurements = []
        for case in evaluation:
            fp.count(len(case["gold"]), 1, 128)
            gold = {fp.text(identity) for identity in case["gold"]}
            if not gold or not gold <= set(values):
                raise ValueError("UniMem held-out gold must name admitted generation sources")
            new = set(_top(values, case["new"], top_k))
            baseline = set(_top(old["vectors"], case["old"], top_k)) if old else set()
            measurements.append(
                {"new_recall": len(new & gold) / len(gold), "old_recall": len(baseline & gold) / len(gold)}
            )
        delta = sum(m["new_recall"] - m["old_recall"] for m in measurements) / len(measurements)
        report = {
            "model": model,
            "recall_delta": delta,
            "queries": len(measurements),
            "measurements": measurements,
            "classification": "held-out retrieval estimate; no deployment guarantee",
            "minimum_delta": minimum_delta,
            "baseline_available": old is not None,
        }
        generation = "embedding-" + fp.digest({"model": model, "vectors": values, "report": report})[:32]
        put(
            root,
            namespace,
            generation,
            {"model": model, "vectors": values, "report": report},
            region=region,
            sources=list(values),
            context=context,
        )
        accepted = delta >= minimum_delta
        if accepted:
            _record(
                root,
                "tier-generations",
                namespace,
                {
                    "generation": generation,
                    "previous_generation": previous.get("generation"),
                    "model": model,
                    "at": fp.now(),
                },
            )
        return {
            **report,
            "accepted": accepted,
            "generation": generation,
            "atomic_pointer": True,
            "namespace_kind": config["kind"],
        }


def rollback(root: str, namespace: str, *, region: str, context: Sequence[str] = ()) -> dict[str, Any]:
    store.operator()
    _namespace(root, namespace, region, context)
    with _jsonl.locked(fp.filename(root, "unimem-tier-generations.jsonl") + ".switch"):
        value = store.latest(root, "tier-generations").get(namespace, {})
        previous = value.get("previous_generation")
        if not previous:
            raise ValueError("UniMem no previous embedding generation")
        old = get(root, namespace, previous, region=region, context=context)
        _record(
            root,
            "tier-generations",
            namespace,
            {"generation": previous, "previous_generation": value["generation"], "model": old["model"], "at": fp.now()},
        )
        return {"namespace": namespace, "generation": previous, "model": old["model"]}


async def native_recall(
    root: str,
    index: Any,
    vector: Sequence[float],
    identities: Sequence[str],
    *,
    context: Sequence[str] = (),
    top_k: int = 10,
) -> list[dict[str, Any]]:
    fp.count(len(identities), 0, 512)
    fp.count(top_k, 1, 100)
    admitted = store.admit(root, identities, context=context)
    hits = await index.search(_vector(vector), top_k=top_k, allowed_ids=sorted(admitted))
    # Native ANN filters are candidate narrowing, never an admission authority.
    final = store.admit(root, [h.key for h in hits], context=context)
    return [{**final[h.key], "score": h.score} for h in hits if h.key in final]


class S3Cold:
    """S3-compatible adapter. Bucket lifecycle/versioned copies need their own deletion policy."""

    def __init__(self, bucket: str, region: str, *, client: Any = None) -> None:
        self.bucket, self.region = fp.text(bucket), fp.text(region)
        if client is None:
            try:
                import boto3
            except ImportError:
                raise RuntimeError("install commontrace[tier] for S3-compatible cold storage") from None
            client = boto3.client("s3", region_name=region)
        self.client = client

    def _check(self, key: str, region: str) -> None:
        if region != self.region:
            raise PermissionError("UniMem S3 backend region mismatch")
        _object_key(key)

    def put(self, key: str, value: bytes, region: str) -> None:
        self._check(key, region)
        if len(value) > store.MAX_BLOB + 1:
            raise ValueError("UniMem cloud payload exceeds the bound")
        self.client.put_object(Bucket=self.bucket, Key=key, Body=value)

    def get(self, key: str, region: str) -> bytes:
        self._check(key, region)
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        body = response["Body"]
        try:
            raw = body.read(store.MAX_BLOB + 2)
        finally:
            body.close()
        if not isinstance(raw, bytes) or len(raw) > store.MAX_BLOB + 1:
            raise ValueError("UniMem cloud payload exceeds the bound")
        return raw

    def delete(self, key: str, region: str) -> None:
        self._check(key, region)
        self.client.delete_object(Bucket=self.bucket, Key=key)


class GCSCold:
    """GCS adapter with operator-pinned bucket residency and bounded downloads."""

    def __init__(self, bucket: str, region: str, *, client: Any = None) -> None:
        self.region = fp.text(region)
        if client is None:
            try:
                from google.cloud import storage
            except ImportError:
                raise RuntimeError("install commontrace[tier] for GCS cold storage") from None
            client = storage.Client()
        self.bucket = client.bucket(fp.text(bucket))

    def _blob(self, key: str, region: str) -> Any:
        if region != self.region:
            raise PermissionError("UniMem GCS backend region mismatch")
        _object_key(key)
        return self.bucket.blob(key)

    def put(self, key: str, value: bytes, region: str) -> None:
        if len(value) > store.MAX_BLOB + 1:
            raise ValueError("UniMem cloud payload exceeds the bound")
        self._blob(key, region).upload_from_string(value)

    def get(self, key: str, region: str) -> bytes:
        raw = self._blob(key, region).download_as_bytes(start=0, end=store.MAX_BLOB + 1)
        if not isinstance(raw, bytes) or len(raw) > store.MAX_BLOB + 1:
            raise ValueError("UniMem cloud payload exceeds the bound")
        return raw

    def delete(self, key: str, region: str) -> None:
        blob = self._blob(key, region)
        if blob.exists():
            blob.delete()


@contextmanager
def access(root: str, namespace: str, *, region: str, context: Sequence[str] = ()) -> Iterator[None]:
    """Establish residency for nested governed reads, never for arbitrary namespaces."""
    config = _namespace(root, namespace, region, context)
    warm(root, namespace, region=region, context=context)
    token = _ACCESS.set((os.path.realpath(root), config["kind"], region))
    try:
        yield
    finally:
        _ACCESS.reset(token)


def assert_read(root: str, kind: str) -> None:
    state = _ACCESS.get()
    if state is None or state[0] != os.path.realpath(root) or state[1] != kind:
        raise PermissionError("UniMem managed tier reads require an admitted residency context")


def recall(
    root: str, namespace: str, vector: Sequence[float], *, region: str, context: Sequence[str] = (), top_k: int = 10
) -> list[dict[str, Any]]:
    """Read the atomic active generation and re-admit every ranked source.

    This bounded reference implementation uses exact cosine search. Managed
    native vector backends use `native_recall` with a namespace-bound index.
    """
    fp.count(top_k, 1, 100)
    _namespace(root, namespace, region, context)
    pointer = store.latest(root, "tier-generations").get(namespace)
    if not pointer:
        raise ValueError("UniMem namespace has no active embedding generation")
    generation = get(root, namespace, pointer["generation"], region=region, context=context)
    admitted = store.admit(root, list(generation["vectors"]), context=context)
    candidates = {identity: generation["vectors"][identity] for identity in admitted}
    ordered = _top(candidates, vector, top_k)
    final = store.admit(root, ordered, context=context)
    return [final[identity] for identity in ordered if identity in final]


def prewarmer(
    root: str, namespace: str, *, region: str, context: Sequence[str] = ()
) -> Callable[[str], dict[str, Any]]:
    """Bind an admitted namespace to session/robot lifecycle callbacks."""
    _namespace(root, namespace, region, context)
    labels = tuple(context)

    def invoke(trigger: str) -> dict[str, Any]:
        return prewarm(root, namespace, region=region, context=labels, trigger=trigger)

    return invoke
