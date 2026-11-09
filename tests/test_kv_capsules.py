from __future__ import annotations

import hashlib

import pytest

from unimem import hierarchical, kv_capsules, privacy_erasure

MODEL = hashlib.sha256(b"audit-model").hexdigest()


def build(root, identity, payload=b"private-kv", **options):
    return kv_capsules.build(root, identity, MODEL, payload, promotion_score=1, promotion_threshold=0.5, **options)


def test_identical_build_retries_are_idempotent(tmp_path):
    root = str(tmp_path)
    memory = seed(root)
    first = build(root, memory.id)
    assert build(root, memory.id) == first
    assert kv_capsules.get(root, memory.id, MODEL) == b"private-kv"


def test_equal_text_in_another_scope_cannot_read_another_sources_opaque_kv(tmp_path):
    root = str(tmp_path)
    private = hierarchical.append_facts(root, [{"statement": "Use the padded tray.", "scopes": ["private"]}])[0][0]
    public = hierarchical.append_facts(root, [{"statement": "Use the padded tray.", "scopes": ["public"]}])[0][0]
    assert private.id != public.id
    first = build(root, private.id, context=["private"])
    with pytest.raises(KeyError):
        kv_capsules.get(root, public.id, MODEL, context=["public"])
    second = build(root, public.id, b"public-kv", context=["public"])
    assert first["id"] != second["id"]
    assert kv_capsules.get(root, public.id, MODEL, context=["public"]) == b"public-kv"


def test_source_revision_changes_invalidate_opaque_kv_even_when_the_text_is_unchanged(tmp_path):
    root = str(tmp_path)
    memory = seed(root)
    build(root, memory.id)
    hierarchical.update_fact(root, memory.id, confidence=0.81)
    with pytest.raises(KeyError):
        kv_capsules.get(root, memory.id, MODEL)


def test_failed_eviction_blocks_reads_and_remains_retryable(tmp_path, monkeypatch):
    root = str(tmp_path)
    memory = seed(root)

    class Flaky(kv_capsules.MemoryPool):
        unavailable = True

        def delete(self, key):
            if self.unavailable:
                raise ConnectionError("delete unavailable")
            super().delete(key)

    pool = Flaky()
    monkeypatch.setitem(kv_capsules._POOLS, "flaky", pool)
    build(root, memory.id, tiers=["flaky"])
    assert kv_capsules.evict(root, memory.id, reason="quarantine")["failed_tiers"] == ["flaky"]
    with pytest.raises(KeyError):
        kv_capsules.get(root, memory.id, MODEL)
    with pytest.raises(ValueError, match="cleanup"):
        build(root, memory.id, tiers=["flaky"])
    pool.unavailable = False
    assert kv_capsules.evict(root, memory.id, reason="quarantine")["failed_tiers"] == []
    assert not pool.values


def test_evicted_capsules_can_be_rebuilt_and_eviction_ignores_old_history(tmp_path, monkeypatch):
    root = str(tmp_path)
    memory = seed(root)
    pool = kv_capsules.MemoryPool()
    monkeypatch.setitem(kv_capsules._POOLS, "counted", pool)
    build(root, memory.id, tiers=["counted"])
    kv_capsules.evict(root, memory.id, reason="quarantine")
    build(root, memory.id, b"new-kv", tiers=["counted"])
    assert kv_capsules.get(root, memory.id, MODEL) == b"new-kv"
    kv_capsules.evict(root, memory.id, reason="quarantine")
    assert kv_capsules.evict(root, memory.id, reason="quarantine")["removed_tiers"] == []


def test_a_put_that_writes_before_raising_is_compensated(tmp_path, monkeypatch):
    root = str(tmp_path)
    memory = seed(root)

    class Partial(kv_capsules.MemoryPool):
        def put(self, key, value):
            super().put(key, value)
            raise ConnectionError("reply lost after put")

    pool = Partial()
    monkeypatch.setitem(kv_capsules._POOLS, "partial", pool)
    with pytest.raises(RuntimeError):
        build(root, memory.id, tiers=["partial"])
    assert not pool.values


@pytest.mark.parametrize("decoded", ["wrong-type", b"", b"x" * 65])
def test_decompressed_payloads_are_bounded_and_typed(tmp_path, monkeypatch, decoded):
    root = str(tmp_path)
    memory = seed(root)

    class InvalidCodec:
        def compress(self, data):
            return b"compressed"

        def decompress(self, data):
            return decoded

    monkeypatch.setattr(kv_capsules, "MAX_KV_BYTES", 64)
    monkeypatch.setitem(kv_capsules._COMPRESSORS, "invalid", InvalidCodec())
    build(root, memory.id, compressor="invalid")
    with pytest.raises(FileNotFoundError):
        kv_capsules.get(root, memory.id, MODEL)


def test_failed_activation_retains_discoverable_staged_ownership(tmp_path, monkeypatch):
    from unimem import frontier_protocol as fp

    root = str(tmp_path)
    memory = seed(root)
    pool = kv_capsules.MemoryPool()
    monkeypatch.setitem(kv_capsules._POOLS, "staged", pool)
    append = fp.append

    def unavailable(root, name, record, **kwargs):
        if name == kv_capsules.INDEX and record["status"] == "active":
            raise OSError("activation unavailable")
        return append(root, name, record, **kwargs)

    monkeypatch.setattr(fp, "append", unavailable)
    with pytest.raises(OSError, match="activation unavailable"):
        build(root, memory.id, tiers=["staged"])
    assert pool.values
    assert kv_capsules.status(root, memory.id)[-1]["status"] == "staging"
    with pytest.raises(KeyError):
        kv_capsules.get(root, memory.id, MODEL)
    assert kv_capsules.evict_sources(root, [memory.id])[0]["failed_tiers"] == []
    assert not pool.values


def test_mutation_during_pool_io_cannot_publish_or_return_stale_kv(tmp_path, monkeypatch):
    root = str(tmp_path)
    memory = seed(root)

    class Mutating(kv_capsules.MemoryPool):
        change_on_get = False
        change_on_put = True

        def put(self, key, value):
            super().put(key, value)
            if self.change_on_put:
                hierarchical.update_fact(root, memory.id, confidence=0.81)

        def get(self, key):
            result = super().get(key)
            if self.change_on_get:
                hierarchical.forget_fact(root, memory.id)
            return result

    pool = Mutating()
    monkeypatch.setitem(kv_capsules._POOLS, "changing", pool)
    with pytest.raises(RuntimeError, match="registration failed"):
        build(root, memory.id, tiers=["changing"])
    assert not pool.values
    pool.change_on_put = False
    build(root, memory.id, tiers=["changing"])
    pool.change_on_get = True
    with pytest.raises(PermissionError):
        kv_capsules.get(root, memory.id, MODEL)


@pytest.mark.parametrize(
    "registry,register,adapter",
    [
        ("_POOLS", "register_pool", kv_capsules.MemoryPool),
        ("_COMPRESSORS", "register_compressor", kv_capsules.IdentityCompression),
    ],
)
def test_versioned_adapter_names_cannot_be_rebound(monkeypatch, registry, register, adapter):
    monkeypatch.setattr(kv_capsules, registry, {})
    install = getattr(kv_capsules, register)
    instance = adapter()
    install("version-1", instance)
    install("version-1", instance)
    with pytest.raises(ValueError, match="immutable"):
        install("version-1", adapter())


def test_memory_tier_retention_is_bounded_and_reads_refresh_lru_order():
    pool = kv_capsules.MemoryPool(max_bytes=6, max_entries=2)
    pool.put("a", b"aa")
    pool.put("b", b"bb")
    assert pool.get("a") == b"aa"
    pool.put("c", b"cccc")
    with pytest.raises(KeyError):
        pool.get("b")
    assert pool.get("a") == b"aa" and pool.get("c") == b"cccc"
    pool.put("a", b"a")
    pool.delete("c")
    pool.put("d", b"ddddd")
    assert pool.get("a") == b"a" and pool.get("d") == b"ddddd"
    with pytest.raises(ValueError, match="byte budget"):
        pool.put("too-large", b"1234567")
    assert pool.get("a") == b"a"  # Invalid writes cannot evict healthy entries.


def test_a_lost_activation_reply_does_not_delete_committed_capsule_bytes(tmp_path, monkeypatch):
    from unimem import frontier_protocol as fp

    root = str(tmp_path)
    memory = seed(root)
    append = fp.append

    def lost_reply(root, name, record, **kwargs):
        result = append(root, name, record, **kwargs)
        if name == kv_capsules.INDEX and record["status"] == "active":
            raise OSError("activation reply lost")
        return result

    monkeypatch.setattr(fp, "append", lost_reply)
    with pytest.raises(OSError, match="activation reply lost"):
        build(root, memory.id)
    assert kv_capsules.get(root, memory.id, MODEL) == b"private-kv"
    monkeypatch.setattr(fp, "append", append)
    assert build(root, memory.id)["status"] == "active"


def test_staging_failure_prevents_any_pool_write(tmp_path, monkeypatch):
    from unimem import frontier_protocol as fp

    root = str(tmp_path)
    memory = seed(root)
    pool = kv_capsules.MemoryPool()
    monkeypatch.setitem(kv_capsules._POOLS, "untouched", pool)

    def unavailable(*_, **__):
        raise OSError("journal unavailable")

    monkeypatch.setattr(fp, "append", unavailable)
    with pytest.raises(OSError, match="journal unavailable"):
        build(root, memory.id, tiers=["untouched"])
    assert not pool.values


def test_failed_registration_cleanup_remains_durable_and_retryable(tmp_path, monkeypatch):
    root = str(tmp_path)
    memory = seed(root)

    class Unavailable(kv_capsules.MemoryPool):
        cleanup_fails = True

        def put(self, key, value):
            super().put(key, value)
            raise ConnectionError("put reply lost")

        def delete(self, key):
            if self.cleanup_fails:
                raise ConnectionError("delete unavailable")
            super().delete(key)

    pool = Unavailable()
    monkeypatch.setitem(kv_capsules._POOLS, "pending", pool)
    with pytest.raises(RuntimeError, match="cleanup is pending") as failure:
        build(root, memory.id, tiers=["pending"])
    assert isinstance(failure.value.__cause__, ConnectionError)
    assert kv_capsules.status(root, memory.id)[-1]["status"] == "eviction_pending"
    with pytest.raises(KeyError):
        kv_capsules.get(root, memory.id, MODEL)
    pool.cleanup_fails = False
    assert kv_capsules.evict_sources(root, [memory.id])[0]["failed_tiers"] == []
    assert not pool.values


def test_memory_tier_eviction_falls_through_to_the_durable_tier(tmp_path, monkeypatch):
    root = str(tmp_path)
    first = seed(root)
    second = hierarchical.append_facts(root, [{"statement": "Inspect robot clearance."}])[0][0]
    pool = kv_capsules.MemoryPool(max_bytes=2, max_entries=1)
    monkeypatch.setitem(kv_capsules._POOLS, "memory", pool)
    build(root, first.id, b"aa")
    build(root, second.id, b"bb")
    assert len(pool.values) == 1
    assert kv_capsules.get(root, first.id, MODEL) == b"aa"
    assert kv_capsules.get(root, second.id, MODEL) == b"bb"


@pytest.mark.parametrize("context", [None, "private", [None], {"scope": "private"}])
def test_capsule_scope_context_rejects_malformed_collections(tmp_path, context):
    root = str(tmp_path)
    memory = seed(root)
    with pytest.raises(ValueError, match="scope labels"):
        build(root, memory.id, context=context)


@pytest.mark.parametrize("model", [None, [], True, "x" * 64])
def test_capsule_model_identity_rejects_invalid_types_and_digests(tmp_path, model):
    with pytest.raises(ValueError, match="fingerprint"):
        kv_capsules.get(str(tmp_path), "source", model)


def test_slow_pool_reads_can_run_concurrently(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    root = str(tmp_path)
    memory = seed(root)
    ready = Barrier(2)

    class Slow(kv_capsules.MemoryPool):
        def get(self, key):
            value = super().get(key)
            ready.wait(timeout=5)
            return value

    monkeypatch.setitem(kv_capsules._POOLS, "parallel", Slow())
    build(root, memory.id, tiers=["parallel"])
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert list(workers.map(lambda _: kv_capsules.get(root, memory.id, MODEL), range(2))) == [b"private-kv"] * 2


@pytest.mark.parametrize("rebuild", [False, True])
def test_eviction_during_a_slow_read_cannot_return_the_revoked_publication(tmp_path, monkeypatch, rebuild):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    root = str(tmp_path)
    memory = seed(root)
    entered, release = Event(), Event()

    class Slow(kv_capsules.MemoryPool):
        def get(self, key):
            value = super().get(key)
            entered.set()
            assert release.wait(5)
            return value

    monkeypatch.setitem(kv_capsules._POOLS, "revocable", Slow())
    build(root, memory.id, tiers=["revocable"])
    with ThreadPoolExecutor(max_workers=1) as workers:
        reading = workers.submit(kv_capsules.get, root, memory.id, MODEL)
        try:
            assert entered.wait(5)
            assert kv_capsules.evict(root, memory.id, reason="quarantine")["failed_tiers"] == []
            if rebuild:
                build(root, memory.id, b"replacement", tiers=["revocable"])
        finally:
            release.set()
        with pytest.raises(KeyError, match="publication"):
            reading.result()
    if rebuild:
        assert kv_capsules.get(root, memory.id, MODEL) == b"replacement"


def test_unrelated_publication_during_a_read_does_not_invalidate_it(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    root = str(tmp_path)
    memory = seed(root)
    other = hierarchical.append_facts(root, [{"statement": "Inspect robot clearance."}])[0][0]
    entered, release = Event(), Event()

    class Slow(kv_capsules.MemoryPool):
        def get(self, key):
            value = super().get(key)
            entered.set()
            assert release.wait(5)
            return value

    monkeypatch.setitem(kv_capsules._POOLS, "parallel-write", Slow())
    build(root, memory.id, tiers=["parallel-write"])
    with ThreadPoolExecutor(max_workers=1) as workers:
        reading = workers.submit(kv_capsules.get, root, memory.id, MODEL)
        try:
            assert entered.wait(5)
            build(root, other.id, b"other-kv", tiers=["parallel-write"])
        finally:
            release.set()
        assert reading.result() == b"private-kv"


def test_warm_reads_do_not_rescan_unchanged_capsule_history(tmp_path, monkeypatch):
    from unimem import frontier_protocol as fp

    root = str(tmp_path)
    memory = seed(root)
    build(root, memory.id)
    assert kv_capsules.get(root, memory.id, MODEL) == b"private-kv"
    journal = fp.journal

    def no_capsule_rescan(root, name):
        assert name != kv_capsules.INDEX
        return journal(root, name)

    monkeypatch.setattr(fp, "journal", no_capsule_rescan)
    for _ in range(3):
        assert kv_capsules.get(root, memory.id, MODEL) == b"private-kv"


def test_build_reply_mutation_cannot_change_cached_routing_metadata(tmp_path):
    root = str(tmp_path)
    memory = seed(root)
    build(root, memory.id)
    reply = build(root, memory.id)
    reply["tiers"].clear()
    reply["source_revision"] = "forged"
    assert kv_capsules.get(root, memory.id, MODEL) == b"private-kv"


def seed(root: str):
    return hierarchical.append_facts(root, [{"statement": "Kitchen helper uses the padded tray."}])[0][0]


def test_capsules_are_tenant_and_model_bound_and_gate_promotion(tmp_path):
    model = hashlib.sha256(b"model-v1").hexdigest()
    root_a, root_b = str(tmp_path / "a"), str(tmp_path / "b")
    memory_a, memory_b = seed(root_a), seed(root_b)
    with pytest.raises(PermissionError, match="promotion threshold"):
        kv_capsules.build(root_a, memory_a.id, model, b"kv", promotion_score=0.4, promotion_threshold=0.5)
    first = kv_capsules.build(
        root_a,
        memory_a.id,
        model,
        b"private-kv",
        promotion_score=0.8,
        promotion_threshold=0.5,
        tiers=("memory", "disk"),
    )
    second = kv_capsules.build(
        root_b, memory_b.id, model, b"private-kv", promotion_score=0.8, promotion_threshold=0.5, tiers=("memory",)
    )
    assert first["id"] != second["id"]
    assert kv_capsules.get(root_a, memory_a.id, model) == b"private-kv"
    assert kv_capsules.get(root_b, memory_b.id, model) == b"private-kv"
    with pytest.raises(KeyError, match="model fingerprint"):
        kv_capsules.get(root_a, memory_a.id, hashlib.sha256(b"model-v2").hexdigest())


def test_retraction_evicts_every_tier_and_erasure_receipt_lists_capsule_tiers(tmp_path):
    root = str(tmp_path)
    memory = seed(root)
    model = hashlib.sha256(b"robot-model").hexdigest()
    kv_capsules.build(
        root,
        memory.id,
        model,
        b"layer-key-values",
        promotion_score=0.9,
        promotion_threshold=0.75,
        tiers=("memory", "disk"),
    )
    receipt = privacy_erasure.purge_fact(root, memory.id, reason="resident requested erasure")
    capsules = [item for item in receipt["deleted_managed_artifacts"] if item["kind"] == "kv-capsules"]
    assert capsules and capsules[0]["capsule_tiers"] == ["disk", "memory"]
    assert capsules[0]["removed_tiers"] == ["disk", "memory"]
    assert capsules[0]["failed_tiers"] == []
    with pytest.raises(PermissionError, match="source is erased"):
        kv_capsules.get(root, memory.id, model)


def test_erasure_events_publish_g20_and_model_upgrade_never_serves_stale_kv(tmp_path):
    from unimem import change_stream
    from unimem import frontier_protocol as fp

    root = str(tmp_path)
    memory = seed(root)
    old_model = hashlib.sha256(b"model-old").hexdigest()
    new_model = hashlib.sha256(b"model-new").hexdigest()
    kv_capsules.build(
        root, memory.id, old_model, b"old-kv", promotion_score=1, promotion_threshold=0.5, tiers=("memory",)
    )
    assert kv_capsules.get(root, memory.id, old_model) == b"old-kv"
    with pytest.raises(KeyError):
        kv_capsules.get(root, memory.id, new_model)
    receipt = kv_capsules.evict(root, memory.id, reason="quarantine")
    assert receipt["removed_tiers"] == ["memory"]
    assert any(
        row["kind"] == "quarantine" and row["target"] == memory.id for row in fp.journal(root, change_stream.JOURNAL)
    )
    assert kv_capsules.status(root, memory.id)[-1]["status"] == "evicted"
