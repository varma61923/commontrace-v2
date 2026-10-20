"""Actual authenticated tenant routes and transactional widening activation."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import select

from commontrace.memory_record import MemoryRecord
from hub import auth, scopes
from hub.db import session_scope
from hub.memory_records import PostgresJournal
from hub.models import MemoryPolicyState
from hub.tests.test_rest import _app, _client, _key, _org_with_key

pytestmark = pytest.mark.asyncio


async def policy_for(factory, org, key):
    async with session_scope(factory) as session:
        principal = await auth.verify_api_key(session, key)
    return {"version": 1, "domain": {
        "principals": [{"id": "key:"+principal.key_id, "class": "admin", "attributes": {}}],
        "actions": ["read", "write", "recall", "export"],
        "memory_classes": ["trace", "fact", "episode"],
        "label_sets": [[], ["tenant:"+org]], "purposes": ["default"]},
        "rules": [{"id": "read", "effect": "allow", "actions": ["read", "recall"],
                   "memory_classes": ["trace", "fact"]}]}


async def post(client, key, operation, body):
    return await client.post("/v1/policy/"+operation, headers=_key(key), json=body)


async def candidate(client, key, policy):
    response = await post(client, key, "certify", {"policy": policy})
    assert response.status_code == 200, response.text
    return response.json()


async def reviewed(client, key, certificate):
    response = await post(client, key, "review",
                          {"certificate": certificate, "reason": "Reviewed exact finite policy."})
    assert response.status_code == 200, response.text
    return response.json()


async def test_widening_review_race_and_cross_tenant_signature(session_factory, config):
    org, key = await _org_with_key(session_factory)
    other, other_key = await _org_with_key(session_factory)
    _, reader = await _org_with_key(session_factory, scopes=[scopes.SCOPE_READ])
    policy = await policy_for(session_factory, org, key)
    cfg = replace(config, ledger_signing_key="test-policy-key-at-least-thirty-two-bytes-long")
    async with _client(_app(session_factory, cfg)) as client:
        assert (await client.post("/v1/policy/current", json={})).status_code == 401
        assert (await post(client, reader, "current", {})).status_code == 403
        certificate = await candidate(client, key, policy)
        assert not certificate["certificate"]["record"]["difference"]["no_widening"]
        assert (await post(client, key, "activate", {"certificate": certificate})).status_code == 403
        review = await reviewed(client, key, certificate)
        assert review["receipt"]["record"]["reviewer"].startswith("key:")
        assert (await post(client, other_key, "activate", {
            "certificate": certificate, "review_receipt": review})).status_code == 403
        forged = deepcopy(certificate)
        forged["certificate"]["record"]["difference"]["no_widening"] = True
        assert (await post(client, key, "activate", {"certificate": forged})).status_code == 403
        statuses = await asyncio.gather(*(post(client, key, "activate", {
            "certificate": certificate, "review_receipt": review}) for _ in range(2)))
        assert sorted(response.status_code for response in statuses) == [200, 409]
        state = (await post(client, key, "current", {})).json()
        assert state["generation"] == 1
        assert (await post(client, other_key, "current", {})).json() == {"policy": None, "generation": 0}
        # A narrowing can activate with no widening review.
        narrowed = deepcopy(policy)
        narrowed["rules"][0]["actions"] = ["read"]
        certificate = await candidate(client, key, narrowed)
        assert certificate["certificate"]["record"]["difference"]["no_widening"]
        assert (await post(client, key, "activate", {"certificate": certificate})).status_code == 200
        assert (await post(client, key, "certify", {"policy": policy, "org_id": other})).status_code == 400
    async with session_scope(session_factory, org_id=org) as session:
        assert (await session.get(MemoryPolicyState, org)).generation == 2
        assert (await session.execute(select(MemoryPolicyState).where(
            MemoryPolicyState.org_id == org))).scalar_one().activation["record"]["generation"] == 2


async def test_active_policy_governs_actual_native_prefilter_and_trace_write(session_factory, config):
    org, key = await _org_with_key(session_factory)
    policy = await policy_for(session_factory, org, key)
    journal = PostgresJournal(session_factory, org)
    await journal.put(MemoryRecord("a-hidden", "episode", 1, {"text": "Wrist wrist calibration."}))
    await journal.put(MemoryRecord("z-visible", "fact", 1, {"text": "Wrist calibration."}))
    cfg = replace(config, ledger_signing_key="test-policy-key-at-least-thirty-two-bytes-long")
    async with _client(_app(session_factory, cfg)) as client:
        certificate = await candidate(client, key, policy)
        review = await reviewed(client, key, certificate)
        assert (await post(client, key, "activate", {
            "certificate": certificate, "review_receipt": review})).status_code == 200
        result = await client.post("/v1/memory/record-search", headers=_key(key), json={"query": "wrist", "limit": 1})
        assert result.status_code == 200, result.text
        assert [row["id"] for row in result.json()["results"]] == ["z-visible"]
        result = await client.post("/v1/memory/record-put", headers=_key(key), json={
            "record": MemoryRecord("new", "episode", 1, {"text": "Disallowed."}).to_dict()})
        assert result.status_code == 403, result.text
        result = await client.post("/api/v1/traces", headers=_key(key), json={
            "title": "Denied", "context_text": "Wrist condition", "solution_text": "Unreviewed write"})
        assert result.status_code == 403, result.text


async def test_missing_signer_and_malformed_policy_request(session_factory, config):
    _, key = await _org_with_key(session_factory)
    async with _client(_app(session_factory, config)) as client:
        assert (await post(client, key, "current", {})).status_code == 503
    cfg = replace(config, ledger_signing_key="test-policy-key-at-least-thirty-two-bytes-long")
    async with _client(_app(session_factory, cfg)) as client:
        for body in ([], {"policy": {}}, {"org_id": "forged"}):
            assert (await post(client, key, "certify", body)).status_code == 400
        assert (await post(client, key, "nonexistent", {})).status_code == 404


@pytest.mark.parametrize("allow_read", [False, True])
async def test_turn_buffer_cannot_bypass_episode_read_policy(session_factory, config, allow_read):
    org, key = await _org_with_key(session_factory)
    journal = PostgresJournal(session_factory, org)
    await journal.capture_turn("private-session", "", text_value="Private historical evidence.", scope={})
    policy = await policy_for(session_factory, org, key)
    # A capture permission does not grant permission to read prior captures.
    policy["rules"].append({"id": "capture", "effect": "allow", "actions": ["write"],
                            "memory_classes": ["episode"]})
    if allow_read:
        policy["rules"][0]["memory_classes"].append("episode")
    cfg = replace(config, ledger_signing_key="test-policy-key-at-least-thirty-two-bytes-long")
    async with _client(_app(session_factory, cfg)) as client:
        certificate = await candidate(client, key, policy)
        review = await reviewed(client, key, certificate)
        assert (await post(client, key, "activate", {
            "certificate": certificate, "review_receipt": review})).status_code == 200
        response = await client.post("/v1/memory/turn", headers=_key(key), json={
            "session_id": "private-session", "query": "evidence", "text": "New capture."})
        assert response.status_code == 200, response.text
        if allow_read:
            assert {row["payload"]["text"] for row in response.json()["buffer"]} == {
                "Private historical evidence.", "New capture."}
        else:
            assert response.json()["buffer"] == []


async def test_policy_migration_forces_tenant_rls_as_non_bypass_reader(session_factory):
    import uuid

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from hub.tests.test_memory_vector_events import _migration

    engine = session_factory.kw["bind"]
    async with engine.connect() as connection:
        permitted = await connection.scalar(
            text("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname=current_user")
        )
    if not permitted:
        pytest.skip("migration enforcement test needs a disposable PostgreSQL role administrator")
    suffix = uuid.uuid4().hex
    schema, reader = "policy_migration_" + suffix, "policy_reader_" + suffix
    org, other = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await connection.execute(text("CREATE TABLE organizations(id UUID PRIMARY KEY)"))
            await connection.execute(
                text("INSERT INTO organizations VALUES (:org),(:other)"), {"org": org, "other": other}
            )
            await connection.run_sync(_migration, "fe36f8a0b2d4_memory_policy_state.py")
            await connection.execute(text(f'CREATE ROLE "{reader}" NOSUPERUSER NOBYPASSRLS'))
            await connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{reader}"'))
            for table in ("memory_policy_states",):
                await connection.execute(text(f'GRANT SELECT,INSERT,DELETE ON {table} TO "{reader}"'))
            await connection.execute(
                text(
                    "INSERT INTO memory_policy_states(org_id) VALUES (:org),(:other)"
                ),
                {"org": org, "other": other},
            )
            forced = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class WHERE relnamespace=current_schema()::regnamespace "
                    "AND relname IN ('memory_policy_states') "
                    "AND relrowsecurity AND relforcerowsecurity"
                )
            )
            assert forced == 1
            await connection.execute(text(f'SET LOCAL ROLE "{reader}"'))
            await connection.execute(text("SELECT set_config('app.org_id',:org,true)"), {"org": org})
            rows = (await connection.execute(text("SELECT org_id::text FROM memory_policy_states"))).scalars().all()
            assert rows == [org]
            with pytest.raises(DBAPIError, match="row-level security"):
                async with connection.begin_nested():
                    await connection.execute(
                        text("INSERT INTO memory_policy_states(org_id) VALUES (:other)"), {"other": other}
                    )
            await connection.execute(text("SELECT set_config('app.org_id','',true)"))
            assert (await connection.execute(text("SELECT org_id FROM memory_policy_states"))).all() == []
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await connection.execute(text(f'DROP ROLE IF EXISTS "{reader}"'))
