from __future__ import annotations

import hashlib

import pytest

from unimem import hierarchical, kv_capsules, privacy_erasure


def seed(root: str):
    return hierarchical.append_facts(root, [{"statement": "Kitchen helper uses the padded tray."}])[0][0]


def test_capsules_are_tenant_and_model_bound_and_gate_promotion(tmp_path):
    model = hashlib.sha256(b"model-v1").hexdigest()
    root_a, root_b = str(tmp_path / "a"), str(tmp_path / "b")
    memory_a, memory_b = seed(root_a), seed(root_b)
    with pytest.raises(PermissionError, match="promotion threshold"):
        kv_capsules.build(root_a, memory_a.id, model, b"kv", promotion_score=.4, promotion_threshold=.5)
    first = kv_capsules.build(root_a, memory_a.id, model, b"private-kv", promotion_score=.8,
                              promotion_threshold=.5, tiers=("memory", "disk"))
    second = kv_capsules.build(root_b, memory_b.id, model, b"private-kv", promotion_score=.8,
                               promotion_threshold=.5, tiers=("memory",))
    assert first["id"] != second["id"]
    assert kv_capsules.get(root_a, memory_a.id, model) == b"private-kv"
    assert kv_capsules.get(root_b, memory_b.id, model) == b"private-kv"
    with pytest.raises(KeyError, match="model fingerprint"):
        kv_capsules.get(root_a, memory_a.id, hashlib.sha256(b"model-v2").hexdigest())


def test_retraction_evicts_every_tier_and_erasure_receipt_lists_capsule_tiers(tmp_path):
    root = str(tmp_path)
    memory = seed(root)
    model = hashlib.sha256(b"robot-model").hexdigest()
    kv_capsules.build(root, memory.id, model, b"layer-key-values", promotion_score=.9,
                      promotion_threshold=.75, tiers=("memory", "disk"))
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
    kv_capsules.build(root, memory.id, old_model, b"old-kv", promotion_score=1,
                      promotion_threshold=.5, tiers=("memory",))
    assert kv_capsules.get(root, memory.id, old_model) == b"old-kv"
    with pytest.raises(KeyError):
        kv_capsules.get(root, memory.id, new_model)
    receipt = kv_capsules.evict(root, memory.id, reason="quarantine")
    assert receipt["removed_tiers"] == ["memory"]
    assert any(row["kind"] == "quarantine" and row["target"] == memory.id
               for row in fp.journal(root, change_stream.JOURNAL))
    assert kv_capsules.status(root, memory.id)[-1]["status"] == "evicted"
