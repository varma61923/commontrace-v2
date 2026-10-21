"""UniMem change streams retain admission and require disconnected consumer ACKs."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from unimem import change_stream as stream
from unimem import erasure_verify, hierarchical, memory_authority, privacy_erasure
from unimem import frontier_protocol as fp


def seed(root, scopes=()):
    return hierarchical.append_facts(
        root, [{"statement": "Private payload: c37b89ce4eb1488ba7b2ae2219d5326d", "scopes": list(scopes)}]
    )[0][0]


def test_standing_query_is_scoped_and_resume_is_cursor_exact(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    a, b = seed(root, ["team:a"]), seed(root, ["team:b"])
    stream.subscribe(root, "reader-a", context=["team:a"])
    stream.publish(root, "assert", a.id, scopes=["team:a"], namespace="facts")
    stream.publish(root, "assert", b.id, scopes=["team:b"], namespace="facts")
    first = stream.poll(root, "reader-a", context=["team:a"])
    assert [e["id"] for e in first["events"]] == [a.id]
    assert stream.poll(root, "reader-a", cursor=first["cursor"], context=["team:a"])["events"] == []
    assert stream.poll(root, "reader-a", context=["team:a"])["events"] == first["events"]
    stream.subscribe(root, "another", context=["team:a"])
    with pytest.raises(ValueError, match="cursor"):
        stream.poll(root, "another", cursor=first["cursor"], context=["team:a"])
    with pytest.raises(PermissionError):
        stream.poll(root, "reader-a", context=["team:b"])
    with memory_authority.writer("other-principal", "operator"):
        with pytest.raises(PermissionError):
            stream.poll(root, "reader-a", context=["team:a"])


def test_erasure_ack_needed_even_when_consumer_disconnected_and_cutoff_allows_ack(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    memory = seed(root)
    stream.subscribe(root, "trainer", ack_seconds=10)
    stream.publish(root, "assert", memory.id)
    delivered = stream.poll(root, "trainer")
    privacy_erasure.purge_fact(root, memory.id, reason="subject erasure")
    erased = stream.publish(root, "erase", memory.id)
    pending = stream.obligations(root)
    assert not pending["all_acknowledged"]
    # A scanner cannot certify while the offline trainer has no erasure ACK.
    verified = erasure_verify.verify(root, [memory.id], strings=[memory.statement])
    assert verified["receipt"] is None and verified["status"] == "unverified"
    removal = stream.poll(root, "trainer", cursor=delivered["cursor"])
    assert removal["events"][0]["kind"] == "erase" and "memory" not in removal["events"][0]
    future = (datetime.fromisoformat(fp.now()) + timedelta(seconds=20)).isoformat()
    monkeypatch.setattr(fp, "now", lambda: future)
    with pytest.raises(PermissionError, match="cut off"):
        stream.poll(root, "trainer", cursor=delivered["cursor"])
    stream.acknowledge(root, "trainer", erased["sequence"], receipt_digest="a" * 64)
    for obligation in stream.obligations(root)["pending"]:
        stream.acknowledge(root, "trainer", obligation["sequence_number"], receipt_digest="b" * 64)
    assert stream.obligations(root)["all_acknowledged"]
    verified = erasure_verify.verify(root, [memory.id], strings=[memory.statement])
    assert verified["receipt"] and fp.verify(root, verified["receipt"])


def test_raw_sqlite_freelist_and_vector_scan_detect_residue(tmp_path):
    root = str(tmp_path)
    path = tmp_path / "old-vectors.sqlite3"
    text = "deleted-sensitive-content-e181c431" * 100
    db = sqlite3.connect(path)
    db.execute("PRAGMA secure_delete=OFF")
    db.execute("CREATE TABLE payload(text TEXT)")
    db.execute("INSERT INTO payload VALUES(?)", (text,))
    db.commit()
    db.execute("DELETE FROM payload")
    db.commit()
    db.close()
    residue = erasure_verify.scan(root, strings=[text[:128]], files=[path.name])
    assert residue["residue"] == [path.name]
    clean = erasure_verify.scan(root, strings=[text[:128]], files=[path.name], vacuum=True)
    assert clean["passed"]
    vector = tmp_path / "vectors.hnsw"
    vector.write_bytes(bytes.fromhex("1234567890abcdef") * 2)
    assert not erasure_verify.scan(root, strings=[], embeddings=["1234567890abcdef"], files=[vector.name])["passed"]
    (tmp_path / "unhandled.vendor").write_bytes(b"data")
    assert erasure_verify.scan(root, strings=[text[:64]], files=["unhandled.vendor"])["unverified"]


def test_mcp_notification_and_sse_diff_are_standard_and_admitted(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    memory = seed(root)
    stream.subscribe(root, "subscriber")
    stream.publish(root, "assert", memory.id)

    class Session:
        def __init__(self):
            self.uris = []

        async def send_resource_updated(self, uri):
            self.uris.append(uri)

    session = Session()
    assert asyncio.run(stream.notify(root, "subscriber", session))
    assert session.uris == ["unimem://beliefs/subscriber"]
    assert stream.notification("subscriber")["method"] == "notifications/resources/updated"
    wire = b"".join(stream.sse(root, "subscriber"))
    assert b"event: beliefs\ndata: " in wire and b"id: " in wire
    assert stream.poll(root, "subscriber")["events"]


def test_crash_after_canonical_commit_recovers_outbox_once(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    memory, _ = hierarchical.add_fact(root, "Inspect original wrist current profile.")
    stream.subscribe(root, "reader")
    with stream.guard(root, "facts"):
        facts = hierarchical.load_facts(root)
        before = stream.snapshot(facts)
        facts[memory.id].statement = "Inspect corrected wrist current profile."
        hierarchical._stamp(facts[memory.id])
        after = stream.snapshot(facts)
        assert stream.stage(root, "facts", before, after)
        hierarchical.save_facts(root, facts)
        with monkeypatch.context() as broken:

            def unavailable(*args, **kwargs):
                raise OSError("publication interrupted")

            broken.setattr(stream, "publish", unavailable)
            assert not stream.complete(root, "facts")
    assert Path(stream.intent_path(root, "facts")).exists()
    result = stream.poll(root, "reader")
    assert len(result["events"]) == 1
    assert "corrected" in result["events"][0]["memory"]["text"]
    assert not Path(stream.intent_path(root, "facts")).exists()
    assert len(fp.journal(root, stream.JOURNAL)) == 1
    stream.poll(root, "reader")
    assert len(fp.journal(root, stream.JOURNAL)) == 1


def test_aborted_outbox_emits_no_phantom_and_symlink_store_is_unverified(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path / "root")
    memory, _ = hierarchical.add_fact(root, "Original admission evidence.")
    stream.subscribe(root, "reader")
    before = stream.snapshot(hierarchical.load_facts(root))
    after = {memory.id: {**before[memory.id], "content_hash": "f" * 64}}
    with stream.guard(root, "facts"):
        stream.stage(root, "facts", before, after)
    assert stream.poll(root, "reader")["events"] == []
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("sensitive-external-subject-data")
    (Path(root) / "link").symlink_to(outside, target_is_directory=True)
    result = erasure_verify.scan(root, strings=["sensitive-external-subject-data"], files=["link/secret.txt"])
    assert result["unverified"] == ["link/secret.txt"] and result["residue"] == []


@pytest.mark.parametrize("sqlite", [False, True])
def test_canonical_writes_publish_through_durable_outbox(tmp_path, monkeypatch, sqlite):
    from unimem import fact_store

    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    if sqlite:
        fact_store.migrate(root)
    stream.subscribe(root, "observer")
    memory = hierarchical.append_facts(root, [{"statement": "Observe current on the right wrist."}])[0][0]
    first = stream.poll(root, "observer")
    assert len(first["events"]) == 1 and first["events"][0]["kind"] == "assert"
    hierarchical.update_fact(root, memory.id, confidence=0.95)
    second = stream.poll(root, "observer", cursor=first["cursor"])
    assert len(second["events"]) == 1 and second["events"][0]["revision"] != first["events"][0]["revision"]
    privacy_erasure.purge_fact(root, memory.id, reason="erasure")
    final = stream.poll(root, "observer", cursor=second["cursor"])
    assert len(final["events"]) == 1 and final["events"][0]["kind"] == "erase"
    assert memory.id not in hierarchical.load_facts(root)


def test_cli_gateway_sse_and_header_resume(tmp_path, monkeypatch, capsys):
    import json

    from unimem import cli, gateway

    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    assert cli.main(["stream", "subscribe", '{"identity":"reader"}', "--dest", root]) == 0
    capsys.readouterr()
    memory, _ = hierarchical.add_fact(root, "Observe calibrated wrist forces.")
    app = gateway.Gateway(root, token="owner", durable=False)
    headers = {"Authorization": "Bearer owner", "Host": "localhost"}
    response = app.handle("GET", "/v1/stream?identity=reader", headers)
    assert response.status == 200 and response.content_type == "text/event-stream"
    wire = b"".join(response.stream).decode()
    cursor = wire.splitlines()[0].removeprefix("id: ")
    payload = json.loads(next(line.removeprefix("data: ") for line in wire.splitlines() if line.startswith("data: ")))
    assert payload["events"][0]["id"] == memory.id
    resumed = app.handle("GET", "/v1/stream?identity=reader", {**headers, "Last-Event-ID": cursor})
    assert resumed.status == 200 and b'"events":[]' in b"".join(resumed.stream)
    assert app.handle("GET", "/v1/stream?identity=reader&limit=bad", headers).status == 400
    assert cli.main(["stream", "tail", '{"identity":"reader"}', "--dest", root]) == 0
    assert json.loads(capsys.readouterr().out)["events"][0]["id"] == memory.id


def test_standing_entity_predicate_diffs_and_deferred_erasure(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    stream.subscribe(root, "robot", entity="right wrist", predicate="environment", namespace="facts")
    memory, _ = hierarchical.add_fact(root, "Measure right wrist current.", category="environment")
    first = stream.poll(root, "robot")
    assert len(first["events"]) == 1
    chunks = stream.render_sse(root, "robot", first)
    privacy_erasure.purge_fact(root, memory.id, reason="owner erasure")
    with pytest.raises(PermissionError, match="changed"):
        next(chunks)
    removal = stream.poll(root, "robot", cursor=first["cursor"])
    assert removal["events"][0]["kind"] == "erase"


def test_mcp_subscription_resource_and_native_notification_handler(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    pytest.importorskip("mcp")
    import mcp_types

    from unimem import mcp_server

    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    server = mcp_server.build_server(root)

    async def run():
        response = await server.call_tool("subscribe_beliefs", {"identity": "consumer"})
        result = json.loads(response.content[0].text)
        assert result["ok"] and result["uri"] == "unimem://beliefs/consumer"
        memory, _ = hierarchical.add_fact(root, "Observe gripper force while lifting.")
        resource = await server.read_resource(result["uri"])
        assert memory.id in str(resource)
        connection = SimpleNamespace(uris=[])

        async def updated(uri):
            connection.uris.append(uri)

        connection.send_resource_updated = updated
        lowlevel = server._lowlevel_server
        handler = lowlevel._request_handlers["resources/subscribe"].handler
        await handler(SimpleNamespace(session=connection), mcp_types.SubscribeRequestParams(uri=result["uri"]))
        for _ in range(30):
            if connection.uris:
                break
            await asyncio.sleep(0.01)
        assert connection.uris == [result["uri"]]
        stop = lowlevel._request_handlers["resources/unsubscribe"].handler
        await stop(SimpleNamespace(session=connection), mcp_types.UnsubscribeRequestParams(uri=result["uri"]))

    asyncio.run(run())


def test_fingerprint_encodings_and_opaque_stores_remain_honest(tmp_path, monkeypatch):
    root = str(tmp_path)
    text = 'private café "customer" identifier'
    for name, encoded in (
        ("utf16.bin", text.encode("utf-16-be")),
        ("escaped.json", __import__("json").dumps(text).encode()),
    ):
        (tmp_path / name).write_bytes(encoded)
        assert erasure_verify.scan(root, strings=[text], files=[name])["residue"] == [name]
    (tmp_path / "encrypted.json").write_text('{"schema":"subject-envelope-v1","ciphertext":"0000"}')
    report = erasure_verify.scan(root, strings=[text], files=["encrypted.json"])
    assert report["unverified"] == ["encrypted.json"] and not report["passed"]
    memory = tmp_path / "memory"
    memory.mkdir(exist_ok=True)
    (memory / "clear.txt").write_text("unrelated data")
    original = erasure_verify._scan_file

    def racing(*args, **kwargs):
        result = original(*args, **kwargs)
        (memory / "new.txt").write_text(text)
        return result

    monkeypatch.setattr(erasure_verify, "_scan_file", racing)
    assert "inventory" in erasure_verify.scan(root, strings=[text])["unverified"]


def test_governed_kafka_forwarding_rechecks_revocation(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    stream.subscribe(root, "broker")
    first, _ = hierarchical.add_fact(root, "Observe wrist current during lifting.")
    second, _ = hierarchical.add_fact(root, "Observe elbow temperature during lifting.")

    class Producer:
        def __init__(self):
            self.events = []

        def produce(self, topic, key, value):
            self.events.append(value)
            privacy_erasure.purge_fact(root, second.id, reason="revoked before next broker write")

        def flush(self, timeout):
            return 0

    producer = Producer()
    with pytest.raises(PermissionError, match="changed"):
        stream.forward_kafka(root, "broker", producer, "beliefs")
    assert len(producer.events) == 1 and first.id.encode() in producer.events[0]


def test_canonical_contradictions_are_recovered_without_cross_scope_pairs(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    stream.subscribe(root, "reader", context=["team:a"])
    older, _ = hierarchical.add_fact(root, "Alice likes spicy food.", scopes=["team:a"])
    newer, _ = hierarchical.add_fact(root, "Alice does not like spicy food.", scopes=["team:a"])
    result = stream.poll(root, "reader", context=["team:a"])
    contradicted = {e["id"] for e in result["events"] if e["kind"] == "contradict"}
    assert contradicted == {older.id, newer.id}
    size = len(fp.journal(root, stream.JOURNAL))
    stream.poll(root, "reader", context=["team:a"])
    assert len(fp.journal(root, stream.JOURNAL)) == size


def test_expired_forged_and_oversized_cursors_fail_closed(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    stream.subscribe(root, "reader")
    cursor = stream.poll(root, "reader")["cursor"]
    with pytest.raises(ValueError):
        stream.poll(root, "reader", cursor=cursor[:-10] + "0000000000")
    with pytest.raises(ValueError):
        stream.poll(root, "reader", cursor="x" * 8193)
    future = (datetime.fromisoformat(fp.now()) + timedelta(days=8)).isoformat()
    monkeypatch.setattr(fp, "now", lambda: future)
    with pytest.raises(ValueError, match="cursor"):
        stream.poll(root, "reader", cursor=cursor)


def test_authority_only_revocation_publishes_a_delivered_erase_obligation(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path)
    stream.subscribe(root, "consumer")
    memory, _ = hierarchical.add_fact(root, "Source-authorized wrist force calibration.")
    initial = stream.poll(root, "consumer")
    memory_authority.record_forgetting(root, memory.id, forgotten=True)
    removal = stream.poll(root, "consumer", cursor=initial["cursor"])
    assert len(removal["events"]) == 1 and removal["events"][0]["kind"] == "erase"
    pending = stream.obligations(root)["pending"]
    assert len(pending) == 1 and pending[0]["sequence_number"] is not None
    stream.acknowledge(root, "consumer", pending[0]["sequence_number"], receipt_digest="c" * 64)
    assert stream.obligations(root)["all_acknowledged"]
