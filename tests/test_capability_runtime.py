"""Runtime transactions keep delegation, canonical bodies and change delivery consistent."""
from __future__ import annotations

import pytest

from tests.test_capabilities import policy
from tests.test_capability_io import authenticate, fixed_time, prepare, proof  # noqa: F401 - shared clock fixture
from unimem import capability_io as io
from unimem import change_stream, lineage, nextwave_store
from unimem.memory_journal import Journal
from unimem.memory_record import MemoryRecord


def test_runtime_class_scope_trust_and_typed_identity(tmp_path):
    from unimem import hierarchical, memory_authority

    root, _ = prepare(tmp_path)
    scope = {"labels": ["project:a"]}
    journal = Journal(root)
    journal.put(MemoryRecord("visible", "fact", 1, {"text": "Wrist calibration."}, scope=scope))
    journal.put(MemoryRecord("hidden", "episode", 1, {"text": "Wrist private episode."}, scope=scope))
    canonical = hierarchical.append_facts(root, [{"statement": "Independent wrist instruction.",
                                                "scopes": ["project:a"]}])[0][0]
    token = io.grant(root, policy(namespaces=["records"], classes=["fact", "directive"]))["token"]
    with io.activate(authenticate(root, token, proof(root, token))):
        assert [record.id for record in journal.search("wrist", scope=scope)] == ["visible"]
        assert journal.current("hidden", scope=scope) is None
        assert journal.latest("hidden") is None and journal.history("hidden") == []
        with pytest.raises(PermissionError):
            journal.put(MemoryRecord("denied", "episode", 1, {"text": "Denied."}, scope=scope))
        with pytest.raises(PermissionError, match="shadow"):
            journal.put(MemoryRecord(canonical.id, "fact", 1, {"text": "Shadow."}, scope=scope))
        record = journal.put(MemoryRecord("instruction", "directive", 1, {"text": "Agent claim."}, scope=scope))
        assert record.status == "quarantined"
        assert record.provenance["authority"] == "agent" and record.provenance["principal"] == "a"
        attestation = lineage.record(root, io.runtime_identity(root, record.id))
        assert attestation["actor"] == "a" and attestation["integrity"] == "agent"
        with memory_authority.writer("spoofed", "operator"):
            assert memory_authority.WRITER.get() == ("a", "agent")
    assert not io.quarantined(root, canonical.id)


def test_committed_runtime_outbox_recovers_once_and_revokes_capture_descendants(tmp_path, monkeypatch):
    root, _ = prepare(tmp_path)
    journal = Journal(root)
    scope = {"labels": ["project:a"]}
    change_stream.subscribe(root, "reader", context=["project:a"], namespace="records")
    token = io.grant(root, policy(namespaces=["records"], classes=["fact", "capture"]))["token"]
    with io.activate(authenticate(root, token, proof(root, token))):
        journal.capture("s", "sensor", {"text": "Wrist current spike."}, scope=scope, event_id="capture")
        journal.put(MemoryRecord("derived", "fact", 1, {"text": "Inspect wrist."}, scope=scope,
                                 relations={"caused_by": ["capture"]}))
    source = io.runtime_identity(root, "capture", capture=True)
    derived = io.runtime_identity(root, "derived")
    assert set(nextwave_store.admit(root, [source, derived], context=["project:a"])) == {source, derived}
    assert not nextwave_store.admit(root, [source, derived], context=["project:b"])
    publish = change_stream.publish
    def crash_after_publish(*args, **kwargs):
        publish(*args, **kwargs)
        raise OSError("simulated post-fsync interruption")
    monkeypatch.setattr(change_stream, "publish", crash_after_publish)
    with pytest.raises(OSError):
        journal.drain_changes()
    monkeypatch.setattr(change_stream, "publish", publish)
    initial = change_stream.poll(root, "reader", context=["project:a"])
    assert len(initial["events"]) == 2
    assert {event["id"] for event in initial["events"]} == {source, derived}
    io.revoke(root, io.check(root, token)["chain_ids"][0], reason="compromised writer", context=["project:a"])
    assert journal.current("derived", scope=scope) is None
    assert journal.buffer("s", scope=scope) == []
    removal = change_stream.poll(root, "reader", cursor=initial["cursor"], context=["project:a"])
    assert {event["id"] for event in removal["events"] if event["kind"] == "quarantine"} == {source, derived}


def test_rolled_back_update_changes_neither_provenance_nor_stream(tmp_path):
    root, _ = prepare(tmp_path)
    journal = Journal(root)
    scope = {"labels": ["project:a"]}
    change_stream.subscribe(root, "reader", context=["project:a"])
    token = io.grant(root, policy(namespaces=["records"], classes=["episode"]))["token"]
    with io.activate(authenticate(root, token, proof(root, token))):
        journal.put(MemoryRecord("episode", "episode", 1, {"text": "Committed."}, scope=scope))
        reference = io.runtime_identity(root, "episode")
        before = lineage.record(root, reference)
        with pytest.raises(ValueError):
            journal.put_many([MemoryRecord("episode", "episode", 2, {"text": "Aborted."}, scope=scope),
                              MemoryRecord("episode", "episode", 2, {"text": "Duplicate version."}, scope=scope)])
        assert journal.current("episode", scope=scope).version == 1
        assert lineage.record(root, reference) == before
    events = change_stream.poll(root, "reader", context=["project:a"])["events"]
    assert len(events) == 1 and "Aborted" not in str(events)


def test_capture_replay_and_capture_only_erase_do_not_change_unrelated_authority(tmp_path):
    from unimem import frontier_protocol as fp
    from unimem import memory_authority

    root, _ = prepare(tmp_path)
    journal = Journal(root)
    scope = {"labels": ["project:a"]}
    change_stream.subscribe(root, "reader", context=["project:a"])
    token = io.grant(root, policy(namespaces=["records"], classes=["capture"]))["token"]
    with io.activate(authenticate(root, token, proof(root, token))):
        journal.capture("s", "sensor", {"text": "Erase this capture."}, scope=scope, event_id="capture")
        reference = io.runtime_identity(root, "capture", capture=True)
        before = lineage.record(root, reference)
        with pytest.raises(ValueError):
            journal.capture("other", "sensor", {"text": "Spoof."}, scope=scope, event_id="capture")
        assert lineage.record(root, reference) == before
        with pytest.raises(PermissionError):
            journal.forget("capture", reason="wrong request operation", scope=scope)
        with pytest.raises(PermissionError):
            memory_authority.record_forgetting(root, reference, forgotten=True)
    initial = change_stream.poll(root, "reader", context=["project:a"])
    authorization = io.authenticate(root, token, proof(root, token, "erase"), request_digest="f"*64,
                                   operation="forget", purpose="support", principal="a")
    with io.activate(authorization):
        journal.forget("capture", reason="consent withdrawn", scope=scope)
    assert not nextwave_store.admit(root, [reference], context=["project:a"])
    assert journal.buffer("s", scope=scope) == []
    removal = change_stream.poll(root, "reader", cursor=initial["cursor"], context=["project:a"])
    assert len(removal["events"]) == 1 and removal["events"][0]["kind"] == "erase"
    assert not change_stream.obligations(root)["all_acknowledged"]
    assert fp.verify(root, fp.sign(root, {"test": "still usable"}))


def test_failed_postcommit_authority_publication_keeps_outbox_and_blocks_reads_until_recovery(tmp_path, monkeypatch):
    root, _ = prepare(tmp_path)
    scope = {"labels": ["project:a"]}
    journal = Journal(root)
    token = io.grant(root, policy(namespaces=["records"], classes=["episode"]))["token"]
    commit = lineage.commit_prepared
    def fail(*args, **kwargs):
        raise OSError("simulated durable journal failure")
    with io.activate(authenticate(root, token, proof(root, token))):
        monkeypatch.setattr(lineage, "commit_prepared", fail)
        record = journal.put(MemoryRecord("committed", "episode", 1, {"text": "Committed payload."}, scope=scope))
        assert record.version == 1
        with pytest.raises(OSError):
            journal.current("committed", scope=scope)
        with journal.connect() as db:
            assert db.execute("SELECT count(*) FROM authority_outbox").fetchone()[0] == 1
        monkeypatch.setattr(lineage, "commit_prepared", commit)
        assert journal.current("committed", scope=scope).version == 1
        with journal.connect() as db:
            assert db.execute("SELECT count(*) FROM authority_outbox").fetchone()[0] == 0
