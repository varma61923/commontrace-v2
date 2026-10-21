"""UniMem cold payloads preserve scope, residency, source freshness and model identity."""

from __future__ import annotations

import asyncio
import time

import pytest

from unimem import hierarchical, safety, tiering
from unimem import nextwave_store as store


def setup(root, *, namespace="robot-a"):
    memory = hierarchical.append_facts(
        root, [{"statement": "Inspect wrist current before loading.", "scopes": ["site:a"]}]
    )[0][0]
    tiering.configure(root, namespace, region="eu", allowed_regions=["eu"], cold_region="eu", scopes=["site:a"])
    tiering.put(
        root, namespace, "payload", {"keyframes": [1, 2, 3]}, region="eu", sources=[memory.id], context=["site:a"]
    )
    return memory


def test_residency_and_scope_guard_every_tier_transition(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    setup(root)
    for operation in (tiering.get, tiering.hibernate, tiering.warm):
        args = (root, "robot-a", "payload") if operation is tiering.get else (root, "robot-a")
        with pytest.raises(PermissionError, match="residency"):
            operation(*args, region="us", context=["site:a"])
        with pytest.raises(PermissionError):
            operation(*args, region="eu", context=["site:b"])
    config = tiering.status(root, "robot-a", region="eu", context=["site:a"])
    with pytest.raises(PermissionError, match="residency"):
        store.get(root, config["kind"], "payload", context=["site:a"])
    assert tiering.hibernate(root, "robot-a", region="eu", context=["site:a"])["state"] == "cold"
    assert tiering.prewarm(root, "robot-a", region="eu", context=["site:a"], trigger="robot-boot")["state"] == "hot"
    start = time.perf_counter()
    assert tiering.get(root, "robot-a", "payload", region="eu", context=["site:a"]) == {"keyframes": [1, 2, 3]}
    assert time.perf_counter() - start < 0.2


def test_protected_source_is_never_hibernated_and_tenant_keys_differ(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path / "a")
    memory = setup(root)
    safety.mark(
        root, memory.id, "condition", safety.review(root, memory.id, reviewer="operator", reason="hazard instruction")
    )
    with pytest.raises(PermissionError, match="protected"):
        tiering.hibernate(root, "robot-a", region="eu", context=["site:a"])
    other = str(tmp_path / "b")
    setup(other)
    assert (
        tiering.status(root, "robot-a", region="eu", context=["site:a"])["kind"]
        != tiering.status(other, "robot-a", region="eu", context=["site:a"])["kind"]
    )


def test_embedding_generation_cutover_report_and_rollback(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    a = setup(root)
    b = hierarchical.append_facts(
        root, [{"statement": "Verify wheel pressure after servicing.", "scopes": ["site:a"]}]
    )[0][0]
    evaluation = [{"new": [1, 0], "old": [1, 0], "gold": [a.id]}]
    first = tiering.reembed(
        root,
        "robot-a",
        "model-1",
        {a.id: [1, 0], b.id: [0, 1]},
        region="eu",
        evaluation=evaluation,
        context=["site:a"],
        top_k=1,
    )
    assert first["accepted"] and first["recall_delta"] == 1
    bad = tiering.reembed(
        root,
        "robot-a",
        "model-bad",
        {a.id: [0, 1], b.id: [1, 0]},
        region="eu",
        evaluation=evaluation,
        context=["site:a"],
        top_k=1,
    )
    assert not bad["accepted"] and bad["recall_delta"] == -1
    second = tiering.reembed(
        root,
        "robot-a",
        "model-2",
        {a.id: [1, 0.1], b.id: [0, 1]},
        region="eu",
        evaluation=evaluation,
        context=["site:a"],
        top_k=1,
    )
    assert second["accepted"] and second["generation"] != first["generation"]
    assert tiering.rollback(root, "robot-a", region="eu", context=["site:a"])["model"] == "model-1"


def test_native_acl_predicate_matches_postfilter_and_readmits_hits(tmp_path):
    from unimem import vector_store

    root = str(tmp_path)
    a = hierarchical.append_facts(root, [{"statement": "Allowed calibration evidence.", "scopes": ["site:a"]}])[0][0]
    b = hierarchical.append_facts(root, [{"statement": "Forbidden calibration evidence.", "scopes": ["site:b"]}])[0][0]

    async def run():
        index = await vector_store.SQLiteVectorIndex.open(
            str(tmp_path / "vectors.sqlite3"), tenant="tenant", namespace="test", model="model", dimension=2
        )
        try:
            await index.upsert(
                [
                    vector_store.VectorRecord(a.id, [1, 0.1], "generation"),
                    vector_store.VectorRecord(b.id, [1, 0], "generation"),
                ]
            )
            all_hits = await index.search([1, 0], top_k=2)
            native = await tiering.native_recall(root, index, [1, 0], [a.id, b.id], context=["site:a"], top_k=1)
            assert [r["id"] for r in native] == [h.key for h in all_hits if h.key == a.id]
        finally:
            await index.close()

    asyncio.run(run())


def test_cloud_adapter_roundtrip_and_cold_erasure(tmp_path, monkeypatch):
    import io

    from unimem import privacy_erasure

    monkeypatch.setattr("os.fsync", lambda _: None)

    class Client:
        def __init__(self):
            self.objects = {}

        def put_object(self, *, Bucket, Key, Body):
            self.objects[Bucket, Key] = Body

        def get_object(self, *, Bucket, Key):
            return {"Body": io.BytesIO(self.objects[Bucket, Key])}

        def delete_object(self, *, Bucket, Key):
            self.objects.pop((Bucket, Key), None)

    client = Client()
    backend = tiering.S3Cold("test-bucket", "eu", client=client)
    monkeypatch.setitem(tiering._BACKENDS, "s3-test", backend)
    root = str(tmp_path)
    memory = hierarchical.append_facts(root, [{"statement": "Private temperature profile for task 8121."}])[0][0]
    tiering.configure(root, "robot", region="eu", allowed_regions=["eu"], cold_region="eu", backend="s3-test")
    tiering.put(root, "robot", "payload", {"profile": "private current data"}, region="eu", sources=[memory.id])
    tiering.hibernate(root, "robot", region="eu")
    assert client.objects
    privacy_erasure.purge_fact(root, memory.id, reason="subject erasure")
    assert not client.objects
    with pytest.raises((ValueError, PermissionError)):
        tiering.get(root, "robot", "payload", region="eu")
    with pytest.raises(PermissionError, match="region"):
        backend.get("tier-" + "a" * 32 + "/" + "b" * 64 + ".json", "us")


def test_failed_cold_copy_keeps_hot_and_warm_cleanup_retries(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    setup(root)
    original = tiering.LocalCold.get
    with monkeypatch.context() as corrupt:
        corrupt.setattr(tiering.LocalCold, "get", lambda *args: b"corrupted")
        with pytest.raises(ValueError, match="round-trip"):
            tiering.hibernate(root, "robot-a", region="eu", context=["site:a"])
        assert tiering.status(root, "robot-a", region="eu", context=["site:a"])["state"] == "hot"
        assert tiering.get(root, "robot-a", "payload", region="eu", context=["site:a"])["keyframes"] == [1, 2, 3]
    assert original
    tiering.hibernate(root, "robot-a", region="eu", context=["site:a"])
    with monkeypatch.context() as failed:

        def unavailable(*args):
            raise OSError("object deletion interrupted")

        failed.setattr(tiering.LocalCold, "delete", unavailable)
        with pytest.raises(OSError):
            tiering.warm(root, "robot-a", region="eu", context=["site:a"])
    assert tiering._state(root, "robot-a")["cleanup_pending"]
    tiering.warm(root, "robot-a", region="eu", context=["site:a"])
    assert not tiering._state(root, "robot-a").get("cleanup_pending")
    assert not list((tmp_path / "memory" / "tier-cold").rglob("*.json"))


def test_idle_policy_and_active_embedding_pointer_drive_recall(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    memory = setup(root)
    assert (
        tiering.hibernate(root, "robot-a", region="eu", context=["site:a"], idle_seconds=3600)["reason"] == "not idle"
    )
    tiering.reembed(
        root,
        "robot-a",
        "model",
        {memory.id: [1, 0.1]},
        region="eu",
        context=["site:a"],
        evaluation=[{"old": [1, 0], "new": [1, 0], "gold": [memory.id]}],
    )
    assert tiering.recall(root, "robot-a", [1, 0], region="eu", context=["site:a"])[0]["id"] == memory.id
    with pytest.raises(ValueError):
        tiering.recall(root, "robot-a", [float("nan"), 0], region="eu", context=["site:a"])


def test_gcs_adapter_bounds_region_and_payload(tmp_path):
    from types import SimpleNamespace

    objects = {}

    class Blob:
        def __init__(self, key):
            self.key = key

        def upload_from_string(self, data):
            objects[self.key] = data

        def download_as_bytes(self, start, end):
            return objects[self.key][start : end + 1]

        def exists(self):
            return self.key in objects

        def delete(self):
            objects.pop(self.key, None)

    client = SimpleNamespace(bucket=lambda name: SimpleNamespace(blob=Blob))
    backend = tiering.GCSCold("bucket", "eu", client=client)
    key = "tier-" + "a" * 32 + "/" + "b" * 64 + ".json"
    backend.put(key, b'{"value":1}', "eu")
    assert backend.get(key, "eu") == b'{"value":1}'
    with pytest.raises(PermissionError):
        backend.get(key, "us")
    with pytest.raises(ValueError):
        backend.put("../escape", b"data", "eu")
    backend.delete(key, "eu")
    assert not objects


def test_direct_managed_writes_require_residency_context(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    memory = setup(root)
    config = tiering.status(root, "robot-a", region="eu", context=["site:a"])
    with pytest.raises(PermissionError, match="residency"):
        store.put(root, config["kind"], "outside", {"secret": "denied"}, scopes=["site:a"], sources=[memory.id])


def test_cli_gateway_and_lifecycle_hooks(tmp_path, monkeypatch, capsys):
    import json

    from unimem import agent_hooks, cli, gateway
    from unimem.client import MemoryClient
    from unimem.robotics.bridge import RobotBridge

    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    setup(root)
    assert (
        cli.main(
            [
                "tier",
                "status",
                json.dumps({"namespace": "robot-a", "region": "eu", "context": ["site:a"]}),
                "--dest",
                root,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["namespace"] == "robot-a"
    app = gateway.Gateway(root, token="owner", durable=False)
    headers = {"Authorization": "Bearer owner", "Host": "localhost"}

    def request(operation, **body):
        return app.handle("POST", "/v1/tier", headers, json.dumps({"operation": operation, "request": body}).encode())

    assert request("status", namespace="robot-a", region="us", context=["site:a"]).status == 403
    assert request("status", namespace="robot-a", region="eu", context=["site:b"]).status == 403
    assert request("status", namespace="robot-a", region="eu", context=["site:a"]).status == 200
    assert request(["invalid"], namespace="robot-a").status == 400
    assert request("status", namespace="robot-a", region="eu", context="site:a").status == 400
    assert (
        request(
            "reembed",
            namespace="robot-a",
            region="eu",
            vectors={"x": [float("nan")]},
            model="bad",
            evaluation=[],
            context=["site:a"],
        ).status
        == 400
    )
    warm = tiering.prewarmer(root, "robot-a", region="eu", context=["site:a"])
    tiering.hibernate(root, "robot-a", region="eu", context=["site:a"])
    MemoryClient(root, prewarm=warm)
    assert tiering._state(root, "robot-a")["state"] == "hot"

    class Transport:
        def post(self, path, body):
            assert tiering._state(root, "robot-a")["state"] == "hot"
            return {"ok": True}

    bridge = RobotBridge(Transport(), embodiment="arm", site="a", skill="pick", robot="robot-a", prewarm=warm)
    tiering.hibernate(root, "robot-a", region="eu", context=["site:a"])
    assert bridge.start("task-1", "wrist profile") == {"ok": True}
    assert {row["trigger"] for row in store.events(root, "tier-prewarm")} == {
        "session-start",
        "robot-boot",
        "task-assignment",
    }
    tiering.configure(root, "agent", region="eu", allowed_regions=["eu"], cold_region="eu", scopes=["agent:a"])
    monkeypatch.setattr(MemoryClient, "reflect", lambda *args, **kwargs: {"ok": True})
    assert agent_hooks.handle(
        root, "start", {"occasion_id": "s1", "tier": {"namespace": "agent", "region": "eu"}}, agent_id="a"
    ) == {"ok": True}
    with pytest.raises(PermissionError):
        agent_hooks.handle(
            root, "start", {"occasion_id": "s2", "tier": {"namespace": "agent", "region": "eu"}}, agent_id="b"
        )


def test_durability_failure_never_withdraws_the_hot_payload(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    setup(root)
    with monkeypatch.context() as broken:

        def interrupted(*args):
            raise OSError("directory synchronization failed")

        broken.setattr(tiering, "_sync_parents", interrupted)
        with pytest.raises(OSError):
            tiering.hibernate(root, "robot-a", region="eu", context=["site:a"])
        assert tiering._state(root, "robot-a")["state"] == "hot"
    assert tiering.get(root, "robot-a", "payload", region="eu", context=["site:a"]) == {"keyframes": [1, 2, 3]}
