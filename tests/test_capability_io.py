"""Persistent capability boundaries exercise real canonical writes and erasure."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from unimem import capabilities as cap
from unimem import capability_io as io
from unimem import frontier_protocol as fp
from unimem import hierarchical, lineage, memory_authority, nextwave_store
from tests.test_capabilities import A_KEY, B_KEY, NOW, policy


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr(fp, "now", lambda: NOW.isoformat())
    monkeypatch.setattr(os, "fsync", lambda _: None)


def prepare(tmp_path):
    root = str(tmp_path / "root")
    for identity, principal, key in (("agent-a", "a", A_KEY), ("agent-b", "b", B_KEY)):
        filename = tmp_path / (identity + ".key")
        filename.write_bytes(key)
        filename.chmod(0o600)
        io.register_agent(root, identity, principal, secret_file=str(filename), clearance=["project:a", "agent:a"])
    return root, io.grant(root, policy())["token"]


def proof(root, token, nonce="fresh", key=A_KEY):
    return cap.prove(token, key=key, algorithm="hmac-sha256", audience=token["links"][-1]["record"]["caveats"]["audience"],
                     tenant=io.tenant(root), request_digest="f"*64, nonce=nonce, now=fp.now())


def authenticate(root, token, signed, **updates):
    return io.authenticate(root, token, signed, request_digest="f"*64, operation="write", purpose="support",
                           principal="a", **updates)


def test_primary_identity_replay_cross_root_and_actual_writer_authority(tmp_path):
    root, token = prepare(tmp_path)
    signed = proof(root, token)
    with pytest.raises(PermissionError, match="principal"):
        io.authenticate(root, token, signed, request_digest="f"*64, operation="write", purpose="support", principal="b")
    authorization = authenticate(root, token, signed)
    with pytest.raises(PermissionError, match="consumed"):
        authenticate(root, token, signed)
    with io.activate(authorization):
        with memory_authority.writer("forged-operator", "operator"):
            assert memory_authority.WRITER.get() == ("a", "agent")
        with pytest.raises(PermissionError):
            nextwave_store.operator()
        with pytest.raises(PermissionError, match="tenant roots"):
            io.current(str(tmp_path / "other"))
        with pytest.raises(PermissionError, match="nested"):
            with io.activate(authorization):
                pass
    assert cap.CURRENT.get() is None


def test_durable_nonce_race_has_one_winner(tmp_path):
    root, token = prepare(tmp_path)
    signed = proof(root, token)
    def attempt(_):
        try:
            authenticate(root, token, signed)
            return True
        except PermissionError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(attempt, range(2))) == 1


@pytest.mark.parametrize("sqlite", [False, True])
def test_canonical_write_class_scope_stamp_and_exact_revocation(tmp_path, sqlite):
    from unimem import change_stream, erasure_verify, fact_store

    root, token = prepare(tmp_path)
    if sqlite:
        fact_store.migrate(root)
    hidden = hierarchical.append_facts(root, [{"statement": "Private wrist preference.", "scopes": ["project:a"],
                                              "memory_type": "preference"}])[0][0]
    unrelated = hierarchical.append_facts(root, [{"statement": "Separate gripper calibration.",
                                                 "scopes": ["project:a"]}])[0][0]
    change_stream.subscribe(root, "reader", context=["project:a"])
    with io.activate(authenticate(root, token, proof(root, token))):
        assert hidden.id not in nextwave_store.admit(root, [hidden.id], context=["project:a"])
        before = hierarchical.load_facts(root)[unrelated.id].to_dict()
        with pytest.raises(PermissionError):
            hierarchical.append_facts(root, [{"statement": "Unauthorized other scope.", "scopes": ["project:b"]}])
        with pytest.raises(KeyError):
            hierarchical.update_fact(root, hidden.id, statement="Unapproved private replacement.")
        memory = hierarchical.append_facts(root, [{"statement": "Inspect wrist before startup.",
                                                  "scopes": ["project:a"]}])[0][0]
        attestation = lineage.record(root, memory.id)
        assert attestation["actor"] == "a" and attestation["integrity"] == "agent"
        assert attestation["delegation"]["chain_digest"] == io.check(root, token)["chain_digest"]
    assert hierarchical.load_facts(root)[unrelated.id].to_dict() == before
    initial = change_stream.poll(root, "reader", context=["project:a"])
    assert any(event["id"] == memory.id for event in initial["events"])
    result = io.revoke(root, io.check(root, token)["chain_ids"][0], reason="compromised agent", context=["project:a"])
    assert memory.id in result["quarantined_previously_admitted"]
    assert memory_authority.lineage_blocked(root, memory.id)
    assert not memory_authority.lineage_blocked(root, unrelated.id)
    assert not memory_authority.lineage_blocked(root, memory.id, canonical_only=True)
    removal = change_stream.poll(root, "reader", cursor=initial["cursor"], context=["project:a"])
    assert any(event["id"] == memory.id and event["kind"] == "quarantine" for event in removal["events"])
    assert change_stream.obligations(root)["all_acknowledged"]
    with pytest.raises(PermissionError, match="canonical"):
        erasure_verify.verify(root, [memory.id], strings=["caller provided fingerprint"])


def test_handoff_is_recipient_bound_one_use_and_sticky(tmp_path):
    root, _ = prepare(tmp_path)
    fact = hierarchical.append_facts(root, [{"statement": "Inspect wrist at startup.", "scopes": ["project:a"]}])[0][0]
    claim = io.handoff(root, [fact.id], "agent-b", context=["project:a"])["claim"]
    token = io.grant(root, policy(audience="agent-b", classes=["general"]))["token"]
    authorization = io.authenticate(root, token, proof(root, token, key=B_KEY), request_digest="f"*64,
                                   operation="read", purpose="support", principal="b")
    with io.activate(authorization):
        bundle = io.receive(root, claim, context=["project:a"])
        assert [memory["id"] for memory in bundle["memories"]] == [fact.id]
        with pytest.raises(PermissionError, match="already redeemed"):
            io.receive(root, claim, context=["project:a"])
    assert io.sticky_labels(root, "b") == ["project:a"]
    wrong = io.grant(root, policy())["token"]
    authorization = io.authenticate(root, wrong, proof(root, wrong, nonce="wrong"), request_digest="f"*64,
                                   operation="read", purpose="support", principal="a")
    with io.activate(authorization):
        with pytest.raises(PermissionError, match="recipient"):
            io.receive(root, claim, context=["project:a"])


def test_gateway_narrows_primary_auth_and_cannot_drop_purpose_or_replay(tmp_path):
    import json

    from unimem import gateway

    root, token = prepare(tmp_path)
    visible = hierarchical.append_facts(root, [{"statement": "Visible wrist calibration.",
                                                "scopes": ["project:a"]}])[0][0]
    hidden = hierarchical.append_facts(root, [{"statement": "Private wrist preference.",
                                               "scopes": ["project:a"], "memory_type": "preference"}])[0][0]
    app = gateway.Gateway(root, token="owner", durable=False)
    target = "/v1/memory/search"
    raw = json.dumps({"query": "wrist", "context": ["project:a"]}).encode()
    def headers(nonce):
        return {"Authorization": "Bearer owner", "Host": "localhost", **cap.headers(
            token, key=A_KEY, holder_algorithm="hmac-sha256", method="POST", target=target, raw=raw,
            purpose="support", nonce=nonce, now=fp.now())}
    supplied = headers("request")
    assert app.handle("POST", target, {**supplied, "Authorization": "Bearer invalid"}, raw).status == 401
    response = app.handle("POST", target, supplied, raw)
    assert response.status == 200
    assert visible.id in response.body.decode() and hidden.id not in response.body.decode()
    assert app.handle("POST", target, supplied, raw).status == 403
    modified = {**headers("purpose"), "X-UniMem-Purpose": "assembly"}
    assert app.handle("POST", target, modified, raw).status == 403
    assert app.handle("POST", target, headers("purpose"), raw).status == 200
    io.register_agent(root, "owner-delegate", "local", clearance=["project:a"])
    assert app.handle("POST", target, {"Authorization": "Bearer owner"}, raw).status == 403
    assert app.handle("GET", "/v1/health", {"Host": "localhost"}).status == 200


def test_revocation_tracks_instrumented_outputs_without_colliding_with_canonical_ids(tmp_path):
    from unimem import contagion

    root, token = prepare(tmp_path)
    unrelated = hierarchical.append_facts(root, [{"statement": "Independent elbow calibration.",
                                                 "scopes": ["project:a"]}])[0][0]
    with io.activate(authenticate(root, token, proof(root, token))):
        parent = hierarchical.append_facts(root, [{"statement": "Inspect wrist mount.", "scopes": ["project:a"]}])[0][0]
    contagion.record_read(root, [parent.id], "a", "episode", unrelated.id, context=["project:a"])
    report = io.revoke(root, io.check(root, token)["chain_ids"][0], reason="compromise", context=["project:a"])
    assert report["observed_output_count"] == 1
    assert io.quarantined(root, "output:" + unrelated.id)
    assert not io.quarantined(root, unrelated.id)


def test_release_requires_valid_independent_authorization_of_current_content(tmp_path):
    root, token = prepare(tmp_path)
    alternate = io.grant(root, policy())["token"]
    with io.activate(authenticate(root, token, proof(root, token))):
        first = hierarchical.append_facts(root, [{"statement": "Inspect wrist mount.", "scopes": ["project:a"]}])[0][0]
        second = hierarchical.append_facts(root, [{"statement": "Inspect elbow mount.", "scopes": ["project:a"]}])[0][0]
    with io.activate(authenticate(root, alternate, proof(root, alternate, "independent"))):
        io.stamp(root, memory_authority.fact_record(first), namespace="facts")
    io.revoke(root, io.check(root, token)["chain_ids"][0], reason="compromised chain", context=["project:a"])
    assert not io.quarantined(root, first.id)
    assert io.quarantined(root, second.id)
    assert lineage.record(root, first.id)["content_hash"] == fp.digest(
        {key: value for key, value in memory_authority.fact_record(first).items() if key != "id"})


def test_request_cache_rechecks_changed_holder_key_before_egress(tmp_path):
    root, token = prepare(tmp_path)
    authorization = authenticate(root, token, proof(root, token))
    with io.activate(authorization):
        assert io.current(root)["principal"] == "a"
        filename = io.agent(root, "agent-a")["secret_file"]
        with open(filename, "wb") as stream:
            stream.write(B_KEY)
        with pytest.raises(PermissionError, match="immutable registration"):
            io.current(root)


def test_alternate_authorization_expiry_invalidates_cached_release(tmp_path, monkeypatch):
    from datetime import timedelta

    root, token = prepare(tmp_path)
    alternate = io.grant(root, policy(expires_at=(NOW + timedelta(seconds=5)).isoformat()))["token"]
    with io.activate(authenticate(root, token, proof(root, token))):
        memory = hierarchical.append_facts(root, [{"statement": "Inspect wrist mount.", "scopes": ["project:a"]}])[0][0]
    with io.activate(authenticate(root, alternate, proof(root, alternate, "independent"))):
        io.stamp(root, memory_authority.fact_record(memory), namespace="facts")
    io.revoke(root, io.check(root, token)["chain_ids"][0], reason="compromise", context=["project:a"])
    assert not io.quarantined(root, memory.id)
    monkeypatch.setattr(fp, "now", lambda: (NOW + timedelta(seconds=6)).isoformat())
    assert io.quarantined(root, memory.id)


@pytest.mark.parametrize("sqlite", [False, True])
def test_delegated_update_rebinds_origin_and_revision_together(tmp_path, sqlite):
    from unimem import fact_store

    root, token = prepare(tmp_path)
    if sqlite:
        fact_store.migrate(root)
    memory = hierarchical.append_facts(root, [{"statement": "Inspect wrist initially.", "scopes": ["project:a"]}])[0][0]
    before = memory.revision
    with io.activate(authenticate(root, token, proof(root, token))):
        updated = hierarchical.update_fact(root, memory.id, statement="Inspect wrist after firmware maintenance.")
        assert updated.revision != before
        assert updated.revision == hierarchical._compute_revision(updated.to_dict())
        assert memory_authority.verify(root, updated.origin, memory_authority.fact_record(updated))
        assert updated.id in nextwave_store.admit(root, [updated.id], context=["project:a"])


def test_high_integrity_ceiling_does_not_grant_unscoped_administration(tmp_path):
    root, _ = prepare(tmp_path)
    filename = tmp_path / "operator.key"
    filename.write_bytes(b"o"*32)
    filename.chmod(0o600)
    io.register_agent(root, "operator-delegate", "administrator", secret_file=str(filename),
                      clearance=["project:a"], authority="operator")
    token = io.grant(root, policy(audience="operator-delegate", delegates=[], labels=["project:a"], ceiling=3))["token"]
    signed = cap.prove(token, key=b"o"*32, algorithm="hmac-sha256", audience="operator-delegate", tenant=io.tenant(root),
                       request_digest="f"*64, nonce="admin", now=fp.now())
    authorization = io.authenticate(root, token, signed, request_digest="f"*64, operation="write", purpose="support",
                                   principal="administrator")
    with io.activate(authorization):
        assert memory_authority.bound_writer()[1] == "operator"
        with pytest.raises(PermissionError, match="undelegated"):
            io.grant(root, policy())


def test_handoff_cannot_redeem_sources_forgotten_after_creation(tmp_path):
    root, _ = prepare(tmp_path)
    memory = hierarchical.append_facts(root, [{"statement": "Inspect wrist mount.", "scopes": ["project:a"]}])[0][0]
    claim = io.handoff(root, [memory.id], "agent-b", context=["project:a"])["claim"]
    hierarchical.forget_fact(root, memory.id)
    token = io.grant(root, policy(audience="agent-b"))["token"]
    authorization = io.authenticate(root, token, proof(root, token, key=B_KEY), request_digest="f"*64,
                                   operation="read", purpose="support", principal="b")
    with io.activate(authorization):
        with pytest.raises(PermissionError, match="unavailable"):
            io.receive(root, claim, context=["project:a"])


def test_external_ed25519_issuer_requires_pin_and_respects_local_maximum_policy(tmp_path):
    ed = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    from tests.test_capabilities import ROOT_KEY

    root, _ = prepare(tmp_path)
    public = ed.Ed25519PrivateKey.from_private_bytes(A_KEY).public_key().public_bytes_raw().hex()
    issuer_public = ed.Ed25519PrivateKey.from_private_bytes(ROOT_KEY).public_key().public_bytes_raw().hex()
    io.register_agent(root, "external-agent", "partner-principal", algorithm="ed25519", public_key=public,
                      clearance=["project:a"])
    rules = policy(audience="external-agent", namespaces=["facts"], delegates=[], labels=["project:a"])
    token = cap.grant(rules, issuer="partner", tenant=io.tenant(root), nonce="external-root", now=fp.now(),
                      algorithm="ed25519", key=ROOT_KEY)
    with pytest.raises(PermissionError, match="trusted pin"):
        io.check(root, token)
    io.pin_issuer(root, "partner", issuer_public, rules)
    assert io.check(root, token)["caveats"]["namespaces"] == ["facts"]
    signed = cap.prove(token, key=A_KEY, algorithm="ed25519", audience="external-agent", tenant=io.tenant(root),
                       request_digest="f"*64, nonce="external-request", now=fp.now())
    authorization = io.authenticate(root, token, signed, request_digest="f"*64, operation="write", purpose="support",
                                   principal="partner-principal")
    with io.activate(authorization):
        memory = hierarchical.append_facts(root, [{"statement": "Inspect wrist calibration.",
                                                  "scopes": ["project:a"]}])[0][0]
        assert lineage.record(root, memory.id)["actor"] == "partner-principal"
    widened = cap.grant({**rules, "namespaces": ["facts", "private"]}, issuer="partner", tenant=io.tenant(root),
                        nonce="widened", now=fp.now(), algorithm="ed25519", key=ROOT_KEY)
    with pytest.raises(PermissionError, match="widen"):
        io.check(root, widened)
