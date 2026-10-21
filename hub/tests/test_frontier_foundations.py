from __future__ import annotations

from dataclasses import replace

import pytest

from unimem import frontier_protocol as fp
from unimem import origin
from unimem import transparency_log as merkle
from hub import transparency
from hub.db import session_scope
from hub.tests.test_rest import _app, _client, _key, _org_with_key

pytestmark = pytest.mark.asyncio


async def test_tenant_log_proofs_and_forgery(session_factory, config):
    first, first_key = await _org_with_key(session_factory)
    second, second_key = await _org_with_key(session_factory)
    config = replace(config, ledger_signing_key="test-signing-key-" + "x" * 32)
    async with session_scope(session_factory, org_id=first) as session:
        receipt = await transparency.append(session, first, {"event": "write"}, secret=config.ledger_signing_key)
    principal = transparency.signer(first, config.ledger_signing_key)
    assert origin.verify(receipt["head"], {principal.id: principal})
    assert merkle.verify_inclusion(merkle.leaf_hash(receipt["statement"]), 0, 1, [],
                                    receipt["head"]["record"]["root_hash"])
    async with _client(_app(session_factory, config)) as client:
        assert (await client.get("/v1/log/head")).status_code == 401
        first_head = (await client.get("/v1/log/head", headers=_key(first_key))).json()
        second_head = (await client.get("/v1/log/head", headers=_key(second_key))).json()
        assert first_head["record"]["tree_size"] == 1
        assert second_head["record"]["tree_size"] == 0
        assert (await client.get("/v1/log/proof?leaf=0", headers=_key(second_key))).status_code == 400
        assert (await client.get("/v1/log/proof?leaf=NaN", headers=_key(first_key))).status_code == 400
        assert (await client.get("/v1/log/consistency?from=0", headers=_key(first_key))).status_code == 200


async def test_incident_freeze_routes_and_tenant_isolation(session_factory, config):
    owner, key = await _org_with_key(session_factory)
    _, other_key = await _org_with_key(session_factory)
    config = replace(config, ledger_signing_key="test-signing-key-" + "x" * 32)
    async with _client(_app(session_factory, config)) as client:
        response = await client.post("/v1/incidents", headers=_key(key),
                                     json={"id": "incident", "valid_time": fp.now()})
        assert response.status_code == 201, response.text
        assert response.json()["hold_id"]
        assert (await client.get("/v1/incidents/incident/timeline", headers=_key(key))).status_code == 200
        assert (await client.get("/v1/incidents/incident/pack", headers=_key(other_key))).status_code == 404
        assert (await client.get("/v1/incidents/console", headers=_key(key))).status_code == 200
        response = await client.post("/v1/incidents", headers=_key(key),
                                     json={"id": "../escape", "valid_time": fp.now()})
        assert response.status_code == 400


async def test_targeted_hub_promotion_restricts_measured_embodiment_and_owner(session_factory, tmp_path):
    from unimem.memory_record import MemoryRecord
    from hub.memory_records import PostgresJournal
    org, _ = await _org_with_key(session_factory)
    other, _ = await _org_with_key(session_factory)
    root = str(tmp_path)
    features = {"gripper": "jaw", "site": "depot"}
    policy = fp.sign(root, {"type": "validated-uplift-policy-v1", "lesson": "memory", "rate_lower": .2,
                            "environment": "real", "classes": {"jaw-depot": features}})
    store = PostgresJournal(session_factory, org)
    record = MemoryRecord("memory", "lesson", 1, {"text": "Use the stable grasp"}, scope={"tenant": org})
    result = await store.put_targeted(record, policy, context="jaw-depot", features=features,
                                     environment="real", verify_policy=lambda p: fp.verify(root, p))
    assert "gripper:jaw" in result.scope["labels"]
    with pytest.raises(PermissionError):
        await store.put_targeted(record, policy, context="jaw-depot", features={**features, "gripper": "suction"},
                                 environment="real", verify_policy=lambda p: fp.verify(root, p))
    with pytest.raises(ValueError, match="tenant"):
        await PostgresJournal(session_factory, other).put_targeted(record, policy, context="jaw-depot",
            features=features, environment="real", verify_policy=lambda p: fp.verify(root, p))


async def test_console_incident_freeze_download_and_scope_checks(session_factory, config):
    from hub.tests.test_console import _app as console_app
    from hub.tests.test_console import _signed_in
    owner, key = await _org_with_key(session_factory)
    _, reader = await _org_with_key(session_factory, name="Reader", scopes=["read"])
    signer = "incident-console-" + "x" * 32
    config = replace(config, ledger_signing_key=signer)
    app = console_app(session_factory=session_factory, signing_key=signer, config=config)
    async with _client(app) as client:
        assert (await client.get("/app/incidents")).status_code == 303
        await _signed_in(client, key)
        page = await client.get("/app/incidents")
        assert page.status_code == 200 and 'name="valid_time"' in page.text
        forged_origin = await client.post("/app/incidents/freeze", headers={"Origin": "https://other.example"},
                                          data={"id": "bad", "valid_time": fp.now()})
        assert forged_origin.status_code == 403
        frozen = await client.post("/app/incidents/freeze", data={"id": "incident", "valid_time": fp.now()})
        assert frozen.status_code == 303
        assert "Download signed evidence" in (await client.get("/app/incidents")).text
        pack = await client.get("/app/incidents/incident/pack")
        assert pack.status_code == 200 and pack.json()["hold_id"]
        assert "attachment" in pack.headers["content-disposition"]
        await _signed_in(client, reader)
        assert (await client.get("/app/incidents/incident/pack")).status_code == 404
        denied = await client.post("/app/incidents/freeze", data={"id": "no", "valid_time": fp.now()})
        assert denied.status_code == 403


async def test_frontier_appends_rebuild_and_detect_tampering(session_factory, config):
    from sqlalchemy import update

    from hub.models import TransparencyEntry, TransparencyHead

    org, key = await _org_with_key(session_factory)
    config = replace(config, ledger_signing_key="test-signing-key-" + "y" * 32)
    secret = config.ledger_signing_key
    principal = transparency.signer(org, secret)
    receipts = []
    for i in range(37):
        async with session_scope(session_factory, org_id=org) as session:
            receipts.append(await transparency.append(session, org, {"event": "write", "n": i}, secret=secret))
    for receipt in receipts:
        head = receipt["head"]["record"]
        assert origin.verify(receipt["head"], {principal.id: principal})
        assert merkle.verify_inclusion(merkle.leaf_hash(receipt["statement"]), receipt["leaf"], head["tree_size"],
                                       receipt["proof"], head["root_hash"])
    # A head written before the frontier column existed is rebuilt from leaf hashes once.
    async with session_scope(session_factory, org_id=org) as session:
        await session.execute(update(TransparencyHead).where(TransparencyHead.org_id == org).values(frontier=None))
    async with session_scope(session_factory, org_id=org) as session:
        receipt = await transparency.append(session, org, {"event": "after-rebuild"}, secret=secret)
    assert receipt["leaf"] == 37 and merkle.verify_inclusion(
        merkle.leaf_hash(receipt["statement"]), 37, 38, receipt["proof"], receipt["head"]["record"]["root_hash"])
    old = receipts[9]["head"]["record"]
    async with _client(_app(session_factory, config)) as client:
        proof = (await client.get("/v1/log/consistency?from=10&to=38", headers=_key(key))).json()
        assert merkle.verify_consistency(10, 38, old["root_hash"], receipt["head"]["record"]["root_hash"],
                                         proof["proof"])
        assert (await client.get("/v1/log/proof?leaf=5", headers=_key(key))).status_code == 200
        async with session_scope(session_factory, org_id=org) as session:
            await session.execute(update(TransparencyEntry).where(
                TransparencyEntry.org_id == org, TransparencyEntry.leaf_index == 5).values(leaf_hash="00" * 32))
        tampered = await client.get("/v1/log/proof?leaf=5", headers=_key(key))
        assert tampered.status_code == 503
        assert tampered.json()["error"] == "transparency_history_unverifiable"


async def test_incident_freeze_pins_evidence_as_of_valid_time(session_factory, config):
    from datetime import datetime, timedelta, timezone

    from hub import audit, incidents

    org, _ = await _org_with_key(session_factory)
    secret = "test-signing-key-" + "z" * 32
    async with session_scope(session_factory, org_id=org) as session:
        await audit.record(session, actor="test", action="before", org_id=org)
    cutoff = datetime.now(timezone.utc)
    async with session_scope(session_factory, org_id=org) as session:
        await audit.record(session, actor="test", action="after", org_id=org)
    async with session_scope(session_factory, org_id=org) as session:
        frozen = await incidents.freeze(session, org, "windowed", cutoff.isoformat(), actor="test", secret=secret)
    record = frozen["snapshot"]["record"]
    actions = [row["action"] for row in record["evidence"]["audit"]]
    assert "before" in actions and "after" not in actions
    assert record["evidence_window"]["audit"]["older_rows_omitted"] is False
    assert all("summary" not in row and "summary_digest" in row for row in record["evidence"]["audit"])
    async with session_scope(session_factory, org_id=org) as session:
        earlier = await incidents.freeze(session, org, "earlier", (cutoff - timedelta(days=1)).isoformat(),
                                         actor="test", secret=secret)
    assert earlier["snapshot"]["record"]["evidence"]["audit"] == []
