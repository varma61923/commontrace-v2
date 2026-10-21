"""Consent purpose boundaries use real canonical admission and erasure."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from unimem import capability_io, hierarchical, memory_authority, nextwave_store, person_io, purpose
from unimem import frontier_protocol as fp
from tests import test_capability_io
from tests.test_capabilities import NOW, policy

HEALTH = purpose.DPV+"ImproveHealthcare"
SHOPPING = purpose.DPV+"ServiceProvision"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(fp, "now", lambda: NOW.isoformat())


def source(tmp_path):
    root = str(tmp_path)
    fact = hierarchical.append_facts(root, [{"statement": "The patient's reviewed exercise limit is ten minutes.",
                                           "scopes": ["project:a"]}])[0][0]
    return root, fact


def enroll(root, ids, **kwargs):
    return purpose.consent(root, "person@example.test", ids, [HEALTH], controller="Care provider",
        expires_at=(NOW+timedelta(minutes=30)).isoformat(), collection_method="Reviewed affirmative consent",
        context=["project:a"], **kwargs)


def admitted(root, identity):
    return identity in nextwave_store.admit(root, [identity], context=["project:a"])


def test_missing_and_mismatched_purpose_never_admit_health_memory(tmp_path):
    root, fact = source(tmp_path)
    assert admitted(root, fact.id)
    receipt = enroll(root, [fact.id])["receipt"]
    assert fp.verify(root, receipt)
    assert not admitted(root, fact.id)
    with purpose.request(root, SHOPPING):
        assert not admitted(root, fact.id)
    with purpose.request(root, HEALTH):
        assert admitted(root, fact.id)
    assert not admitted(root, fact.id)
    assert "person@example.test" not in fp.wire(purpose._state(root)).decode()


def test_verified_capability_purpose_cannot_be_overridden_by_an_agent(tmp_path):
    root, _ = test_capability_io.prepare(tmp_path)
    fact = hierarchical.append_facts(root, [{"statement": "Reviewed exercise limit.", "scopes": ["project:a"]}])[0][0]
    enroll(root, [fact.id])
    token = capability_io.grant(root, policy(purposes=[SHOPPING]))["token"]
    auth = capability_io.authenticate(root, token, test_capability_io.proof(root, token), request_digest="f"*64,
        operation="read", purpose=SHOPPING, principal="a")
    with capability_io.activate(auth):
        assert not admitted(root, fact.id)
        with pytest.raises(PermissionError, match="differs"):
            with purpose.request(root, HEALTH):
                pass
        with pytest.raises(PermissionError):
            purpose.explain(root, "person@example.test", context=["project:a"])
    with memory_authority.writer("agent", "agent"):
        with pytest.raises(PermissionError, match="authenticated"):
            with purpose.request(root, HEALTH):
                pass


def test_owner_explanation_includes_inferred_memories_without_agent_grant(tmp_path):
    root, first = source(tmp_path)
    inferred = hierarchical.append_facts(root, [{"statement": "The person may prefer short exercise sessions.",
                                                "scopes": ["project:a"]}])[0][0]
    enroll(root, [first.id, inferred.id], inferred=[inferred.id])
    view = purpose.explain(root, "person@example.test", context=["project:a"])
    assert {v["id"] for v in view["memories"]} == {first.id, inferred.id}
    assert next(v for v in view["memories"] if v["id"] == inferred.id)["inferred"]
    assert all(v["purposes"] == [HEALTH] for v in view["memories"])
    assert not purpose.explain(root, "someone-else", context=["project:a"])["memories"]
    assert not purpose.explain(root, "person@example.test", context=["project:other"])["memories"]
    assert not admitted(root, first.id)


def test_expiry_blocks_reads_before_cleanup_and_missing_vault_stays_pending(tmp_path, monkeypatch):
    root, fact = source(tmp_path)
    enroll(root, [fact.id])
    monkeypatch.setattr(fp, "now", lambda: (NOW+timedelta(minutes=31)).isoformat())
    with purpose.request(root, HEALTH):
        assert not admitted(root, fact.id)
    assert person_io._fact(root, fact.id) is not None
    result = purpose.reap(root)["expired"][0]
    assert result["status"] == "pending" and result["certificate"] is None
    assert result["local_purge"]
    assert person_io._fact(root, fact.id) is None
    assert memory_authority.lineage_blocked(root, fact.id)


def test_expiry_uses_actual_subject_vault_and_signed_erasure_certificate(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from unimem import certified_erasure

    root, fact = source(tmp_path/"store")
    receipt = enroll(root, [fact.id], vault_directory=str(tmp_path/"vault"))["receipt"]["record"]
    _, kind, capsule_id = receipt["envelopes"][fact.id].split(":", 2)
    with purpose.request(root, HEALTH):
        envelope = nextwave_store.get(root, kind, capsule_id, context=["project:a"])["envelope"]
    vault = certified_erasure.SubjectVault(root, str(tmp_path/"vault"))
    assert fact.statement in vault.open("consent:"+receipt["id"], envelope)
    monkeypatch.setattr(fp, "now", lambda: (NOW+timedelta(minutes=31)).isoformat())
    result = purpose.reap(root)["expired"][0]
    assert result["status"] == "complete"
    assert certified_erasure.verify(root, result["certificate"])
    with pytest.raises(PermissionError, match="destroyed"):
        vault.open("consent:"+receipt["id"], envelope)
    assert person_io._fact(root, fact.id) is None
    assert purpose.reap(root)["expired"][0] == result
    body = result["certificate"]["certificate"]["record"]
    assert body["remote_erasure"] is False
    assert "pre-encrypted" in body["backup_scope"]


def test_signed_receipt_tampering_and_rebinding_fail_closed(tmp_path):
    root, fact = source(tmp_path)
    enroll(root, [fact.id])
    with pytest.raises(PermissionError, match="rebound"):
        enroll(root, [fact.id])
    path = fp.filename(root, "consent-state.json")
    with open(path) as stream:
        signed = json.load(stream)
    next(iter(signed["record"]["receipts"].values()))["purposes"] = [SHOPPING]
    with open(path, "w") as stream:
        json.dump(signed, stream)
    with purpose.request(root, SHOPPING):
        with pytest.raises(PermissionError, match="signature"):
            admitted(root, fact.id)


def test_withdrawal_is_immediate_and_never_falls_back_to_legacy_admission(tmp_path):
    root, fact = source(tmp_path)
    receipt = enroll(root, [fact.id])["receipt"]["record"]
    purpose.withdraw(root, receipt["id"])
    with purpose.request(root, HEALTH):
        assert not admitted(root, fact.id)
    assert purpose.scope(root, fact.id)["status"] == "expired-or-withdrawn"


def test_passport_requires_disclosure_consent_and_preserves_receipt(tmp_path):
    pytest.importorskip("cryptography")
    root, fact = source(tmp_path)
    enroll(root, [fact.id])
    with purpose.request(root, SHOPPING):
        with pytest.raises(PermissionError):
            person_io.export(root, [fact.id], context=["project:a"], model_digest="sha256:"+"a"*64)
    with purpose.request(root, HEALTH):
        result = person_io.export(root, [fact.id], context=["project:a"], model_digest="sha256:"+"a"*64)
    manifest = person_io.verify_passport(root, result["bundle"])
    consent = manifest["components"]["semantic"][0]["consent_scope"]
    assert consent["purposes"] == [HEALTH] and fp.verify(root, consent["receipt"])


def test_gateway_recall_filters_even_supplied_items_by_exact_purpose(tmp_path):
    from unimem.gateway import Gateway
    from tests.test_gateway import call

    root, fact = source(tmp_path)
    enroll(root, [fact.id])
    gateway = Gateway(root, token="test-token-very-secret")
    for selected, expected in [(SHOPPING, []), (HEALTH, [fact.id])]:
        status, result = call(gateway, "POST", "/v1/recall", {"occasion_id": "episode-"+selected.rsplit('#', 1)[1],
            "purpose": selected, "items": [{"id": fact.id, "text": fact.statement}]},
            headers={"Authorization": "Bearer test-token-very-secret", "Host": "localhost"})
        assert status == 200, result
        assert [item["id"] for item in result["deliver"]] == expected


def test_native_records_and_capture_buffers_share_purpose_admission(tmp_path):
    from unimem.memory_journal import Journal
    from unimem.memory_record import MemoryRecord

    root = str(tmp_path)
    journal = Journal(root)
    labels = {"labels": ["project:a"]}
    journal.put(MemoryRecord("preference", "profile", 1, {"text": "Short care sessions."}, scope=labels))
    journal.capture("session", "message", {"text": "Reviewed care information."}, scope=labels, event_id="message")
    enroll(root, ["record:preference", "capture:message"])
    with purpose.request(root, SHOPPING):
        assert journal.current("preference", scope=labels) is None
        assert journal.buffer("session", scope=labels) == []
    with purpose.request(root, HEALTH):
        assert journal.current("preference", scope=labels) is not None
        assert journal.buffer("session", scope=labels)[0]["id"] == "message"


def test_expiry_emits_real_stream_erasure_and_requires_downstream_ack(tmp_path, monkeypatch):
    from unimem import change_stream

    root, fact = source(tmp_path)
    change_stream.subscribe(root, "downstream", context=["project:a"])
    change_stream.publish(root, "assert", fact.id, scopes=["project:a"], namespace="facts")
    change_stream.poll(root, "downstream", context=["project:a"])
    enroll(root, [fact.id])
    monkeypatch.setattr(fp, "now", lambda: (NOW+timedelta(minutes=31)).isoformat())
    purpose.reap(root)
    events = change_stream.poll(root, "downstream", context=["project:a"])["events"]
    assert any(row["kind"] == "erase" and row["id"] == fact.id for row in events)
    assert not change_stream.obligations(root)["all_acknowledged"]


def test_read_consent_alone_never_grants_export_or_inference(tmp_path):
    pytest.importorskip("cryptography")
    from unimem import policy_io

    root, fact = source(tmp_path)
    enroll(root, [fact.id], processing=[purpose.DPV+"Retrieve", purpose.DPV+"Store"])
    with purpose.request(root, HEALTH):
        assert admitted(root, fact.id)
        with pytest.raises(PermissionError):
            person_io.export(root, [fact.id], context=["project:a"], model_digest="sha256:"+"a"*64)
        assert not policy_io.permits(root, {"id": "derived", "source_traces": [fact.id]}, action="write")


def test_credentials_expire_with_consent_and_withdrawal_revokes_live_status(tmp_path):
    pytest.importorskip("cryptography")
    from unimem import credentials

    root, fact = source(tmp_path)
    receipt = enroll(root, [fact.id])["receipt"]["record"]
    person_io.configure_credentials(root, "https://issuer.example.test/status/1")
    with purpose.request(root, HEALTH):
        issued = person_io.issue(root, fact.id, credentials.public_key(bytes(range(32))).hex(),
            "sha256:"+"a"*64, context=["project:a"])
    assert credentials._parts(issued["issued"].split("~")[0])[1]["exp"] == int(
        (NOW+timedelta(minutes=30)).timestamp())
    assert 0 in person_io._active_credentials(root, int(NOW.timestamp()))[1]
    purpose.withdraw(root, receipt["id"])
    assert 0 not in person_io._active_credentials(root, int(NOW.timestamp()))[1]


def test_scoped_owner_erasure_does_not_require_a_processing_purpose(tmp_path):
    from unimem import record_api
    from unimem.memory_journal import Journal
    from unimem.memory_record import MemoryRecord

    root = str(tmp_path)
    journal = Journal(root)
    journal.put(MemoryRecord("private", "profile", 1, {"text": "Private care preference."},
                             scope={"agent": "owner"}))
    purpose.consent(root, "person", ["record:private"], [HEALTH], controller="Care provider",
        expires_at=(NOW+timedelta(minutes=30)).isoformat(), collection_method="Affirmative consent",
        context=["agent:owner"])
    assert journal.history("private") == []
    with pytest.raises(PermissionError):
        record_api.operate(root, "record-forget", {"id": "private", "reason": "Other requester."},
                           context=["agent:other"], untrusted=True)
    result = record_api.operate(root, "record-forget", {"id": "private", "reason": "Owner erasure."},
                               context=["agent:owner"], untrusted=True)
    assert "private" in result["deleted"]


def test_cli_enrollment_explanation_and_actual_mcp_owner_view(tmp_path, capsys):
    from unimem import cli

    root, fact = source(tmp_path)
    body = {"subject": "person", "identities": [fact.id], "purposes": [HEALTH], "controller": "Care provider",
        "expires_at": (NOW+timedelta(minutes=30)).isoformat(), "collection_method": "Affirmative consent",
        "context": ["project:a"], "inferred": [fact.id]}
    assert cli.main(["person", "consent", json.dumps(body), "--dest", root]) == 0
    assert json.loads(capsys.readouterr().out)["receipt"]["record"]["sources"][fact.id]["inferred"]
    assert cli.main(["person", "explain", json.dumps({"subject": "person", "context": ["project:a"]}),
                     "--dest", root]) == 0
    assert json.loads(capsys.readouterr().out)["memories"][0]["id"] == fact.id
    pytest.importorskip("mcp")
    from unimem import mcp_server
    from tests.test_mcp_server import call

    server = mcp_server.build_server(root, allow_approval=False)
    result = call(server, "explain_memory_about_me", subject="person", context=["project:a"])
    assert result["ok"] and result["memories"][0]["inferred"]


def test_native_purpose_filter_precedes_ranking_and_result_limit(tmp_path):
    from unimem.memory_journal import Journal
    from unimem.memory_record import MemoryRecord

    root = str(tmp_path)
    journal = Journal(root)
    scope = {"labels": ["project:a"]}
    journal.put(MemoryRecord("a-hidden", "episode", 1, {"text": "Wrist wrist calibration."}, scope=scope))
    journal.put(MemoryRecord("z-visible", "fact", 1, {"text": "Wrist calibration."}, scope=scope))
    enroll(root, ["record:a-hidden"])
    with purpose.request(root, SHOPPING):
        assert [row.id for row in journal.search("wrist", scope=scope, limit=1)] == ["z-visible"]


def test_expiry_purges_native_sources_lessons_and_traces(tmp_path, monkeypatch):
    from pathlib import Path

    from unimem import lesson_io, paths, trace_io
    from unimem.memory_journal import Journal
    from unimem.memory_record import MemoryRecord

    root = str(tmp_path)
    journal = Journal(root)
    labels = {"labels": ["project:a"]}
    journal.put(MemoryRecord("private", "profile", 1, {"text": "Private care preference."}, scope=labels))
    lesson = Path(paths.lessons_dir(root))/"lesson_care.md"
    lesson.parent.mkdir(parents=True, exist_ok=True)
    lesson_io.write_lesson(str(lesson), {"name": "care", "status": "active", "scopes": ["project:a"]},
                           "Reviewed care instruction.", root=root)
    trace = trace_io.write_new(root, title="Care session", context="Reviewed care context.",
                              solution="Short sessions.", tags=[], extra={"scopes": ["project:a"]})
    trace_id = trace_io.read(trace)[0]["id"]
    enroll(root, ["record:private", "care", trace_id])
    monkeypatch.setattr(fp, "now", lambda: (NOW+timedelta(minutes=31)).isoformat())
    result = purpose.reap(root)["expired"][0]
    assert result["local_purge"]
    assert not lesson.exists() and not Path(trace).exists()
    with journal.connect() as db:
        assert db.execute("SELECT 1 FROM records WHERE id='private'").fetchone() is None


def test_gateway_capability_purpose_is_bound_to_headers_and_body(tmp_path):
    from unimem import capabilities, gateway
    from tests.test_capabilities import A_KEY

    root, _ = test_capability_io.prepare(tmp_path)
    fact = hierarchical.append_facts(root, [{"statement": "Private care limit.", "scopes": ["project:a"]}])[0][0]
    enroll(root, [fact.id])
    token = capability_io.grant(root, policy(purposes=[SHOPPING]))["token"]
    server = gateway.Gateway(root, token="gateway-owner-secret")
    for body_purpose, expected_status in [(SHOPPING, 200), (HEALTH, 403)]:
        raw = json.dumps({"occasion_id": "test-"+body_purpose.rsplit('#', 1)[1],
            "purpose": body_purpose, "items": [{"id": fact.id, "text": fact.statement}]}).encode()
        headers = capabilities.headers(token, key=A_KEY, holder_algorithm="hmac-sha256", method="POST",
            target="/v1/recall", raw=raw, purpose=SHOPPING, nonce=body_purpose, now=fp.now())
        headers.update({"Authorization": "Bearer gateway-owner-secret", "Host": "localhost"})
        response = server.handle("POST", "/v1/recall", headers, raw)
        result = json.loads(response.body)
        assert response.status == expected_status, result
        if response.status == 200:
            assert result["deliver"] == []
