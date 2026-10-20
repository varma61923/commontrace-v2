"""Delegation narrows primary Hub auth, shares replay state and filters before ranking."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from commontrace import capabilities as cap
from commontrace import capability_io as io
from commontrace import frontier_protocol as fp
from commontrace import lineage, paths
from commontrace.memory_record import MemoryRecord
from hub import auth, crud, delegation
from hub.db import session_scope
from hub.memory_records import PostgresJournal
from hub.models import CapabilityNonce, MemoryRecordProjection, Trace
from hub.tests.test_rest import _app, _org_with_key
from tests.test_capabilities import A_KEY, policy

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("encoded", [
    b'{"record":{"kind":"narrow"},"record":{"kind":"wide"}}',
    b'{"record":{"limit":NaN}}',
    b'{"record":{"limit":Infinity}}',
])
async def test_capability_header_rejects_ambiguous_json(encoded):
    with pytest.raises(ValueError):
        delegation._decode(base64.urlsafe_b64encode(encoded).decode().rstrip("="), 8192)


@pytest.mark.parametrize("raw", [
    b'{"method":"tools/call","method":"tools/list","params":{"name":"delete_trace"}}',
    b'{"method":"tools/call","params":{"name":"delete_trace"},"noise":NaN}',
])
async def test_delegated_mcp_operation_rejects_ambiguous_json(raw):
    request = SimpleNamespace(url=SimpleNamespace(path="/mcp"), method="POST")
    config = SimpleNamespace(streamable_http_path="/mcp")
    with pytest.raises(ValueError):
        delegation._operation(request, raw, config)


async def setup(
    session_factory, config, tmp_path, *, operations=None, namespaces=None, classes=None, primary_scopes=None
):
    org, raw_key = await _org_with_key(session_factory, scopes=primary_scopes)
    async with session_scope(session_factory) as session:
        authenticated = await auth.verify_api_key(session, raw_key)
    directory = str(tmp_path / "delegation")
    root = os.path.join(directory, org)
    filename = tmp_path / "agent.key"
    filename.write_bytes(A_KEY)
    filename.chmod(0o600)
    labels = ["project:a", "tenant:" + org]
    io.register_agent(root, "agent-a", authenticated.key_prefix, secret_file=str(filename), clearance=labels)
    rules = policy(
        namespaces=namespaces or ["traces", "records"],
        classes=classes or ["general", "fact"],
        operations=operations or ["read", "write", "forget"],
        labels=labels,
        delegates=[],
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    token = io.grant(root, rules, tenant_id=org)["token"]
    return org, raw_key, root, token, replace(config, capability_directory=directory), authenticated


def headers(raw_key, token, target, raw, nonce, *, purpose="support"):
    return {
        "X-API-Key": raw_key,
        **cap.headers(
            token,
            key=A_KEY,
            holder_algorithm="hmac-sha256",
            method="POST",
            target=target,
            raw=raw,
            purpose=purpose,
            nonce=nonce,
            now=fp.now(),
        ),
    }


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_rest_scope_primary_identity_purpose_and_replay_across_replicas(
    session_factory, config, tmp_path, monkeypatch
):
    org, raw_key, root, token, configured, _ = await setup(session_factory, config, tmp_path)
    other_org, other_key = await _org_with_key(session_factory, "Other")
    async with session_scope(session_factory, org_id=org) as session:
        session.add_all(
            [
                Trace(
                    id="00000000-0000-0000-0000-000000000001",
                    org_id=org,
                    title="visible",
                    context_text="wrist",
                    solution_text="wrist calibration",
                    agent_type="robot",
                    scopes=["project:a"],
                ),
                Trace(
                    id="00000000-0000-0000-0000-000000000002",
                    org_id=org,
                    title="private",
                    context_text="wrist",
                    solution_text="private wrist instruction",
                    agent_type="robot",
                    scopes=["project:b"],
                ),
            ]
        )
    from hub import search as hub_search

    monkeypatch.setattr(hub_search, "RANK_BUDGET", 1)
    target, raw = "/api/v1/traces/search", b'{"q":"wrist","limit":20}'
    signed = headers(raw_key, token, target, raw, "first")
    async with client(_app(session_factory, configured)) as first, client(_app(session_factory, configured)) as replica:
        assert (await first.post(target, headers={**signed, "X-API-Key": "invalid"}, content=raw)).status_code == 401
        response = await first.post(target, headers=signed, content=raw)
        assert response.status_code == 200, response.text
        assert [trace["title"] for trace in response.json()["results"]] == ["visible"]
        assert (await replica.post(target, headers=signed, content=raw)).status_code == 403
        assert (await first.post(target, headers={"X-API-Key": raw_key}, content=raw)).status_code == 403
        altered = {**headers(raw_key, token, target, raw, "purpose"), "X-UniMem-Purpose": "assembly"}
        assert (await first.post(target, headers=altered, content=raw)).status_code == 403
        assert (
            await first.post(target, headers=headers(raw_key, token, target, raw, "purpose"), content=raw)
        ).status_code == 200
        cross = {**headers(raw_key, token, target, raw, "cross"), "X-API-Key": other_key}
        assert (await replica.post(target, headers=cross, content=raw)).status_code == 403
    assert not cap.CURRENT.get() and not delegation.ROOT.get()
    assert not os.path.exists(os.path.join(paths.memory_dir(root), ".capability-nonces.sqlite3"))
    async with session_scope(session_factory, org_id=org) as session:
        assert (
            len((await session.execute(select(CapabilityNonce).where(CapabilityNonce.org_id == org))).scalars().all())
            == 2
        )
    assert other_org != org


async def test_shared_nonce_concurrent_consumption_has_one_winner(session_factory, config, tmp_path):
    _, raw_key, _, token, configured, _ = await setup(session_factory, config, tmp_path)
    target, raw = "/api/v1/traces/search", b'{"q":"wrist"}'
    signed = headers(raw_key, token, target, raw, "race")
    async with client(_app(session_factory, configured)) as a, client(_app(session_factory, configured)) as b:
        responses = await asyncio.gather(
            a.post(target, headers=signed, content=raw), b.post(target, headers=signed, content=raw)
        )
    assert sorted(response.status_code for response in responses) == [200, 403]


async def test_hub_write_stamps_committed_lineage_and_revocation_blocks_new_reads(session_factory, config, tmp_path):
    org, raw_key, root, token, configured, _ = await setup(session_factory, config, tmp_path)
    target = "/api/v1/traces"
    raw = json.dumps(
        {
            "title": "Inspect wrist",
            "context_text": "Wrist current high",
            "solution_text": "Inspect mount",
            "scopes": ["project:a"],
        }
    ).encode()
    async with client(_app(session_factory, configured)) as app:
        response = await app.post(target, headers=headers(raw_key, token, target, raw, "write"), content=raw)
        assert response.status_code == 201, response.text
        identity = response.json()["id"]
        async with session_scope(session_factory, org_id=org) as session:
            trace = await session.get(Trace, identity)
            assert trace.extensions["profile"]["delegation"]["chain_digest"] == fp.digest(token)
            assert trace.extensions["profile"]["origin"]["authority"] == "agent"
        assert lineage.record(root, identity)["integrity"] == "agent"
        io.revoke(
            root, io.check(root, token, tenant_id=org)["chain_ids"][0], reason="compromise", context=["project:a"]
        )
        raw = b'{"q":"wrist"}'
        response = await app.post(
            "/api/v1/traces/search",
            headers=headers(raw_key, token, "/api/v1/traces/search", raw, "revoked"),
            content=raw,
        )
        assert response.status_code == 403


async def test_capability_cannot_fetch_other_scope_by_id(session_factory, config, tmp_path):
    org, _, root, token, _, authenticated = await setup(session_factory, config, tmp_path)
    identity = "00000000-0000-0000-0000-000000000003"
    async with session_scope(session_factory, org_id=org) as session:
        session.add(
            Trace(
                id=identity,
                org_id=org,
                title="hidden",
                context_text="private",
                solution_text="private",
                agent_type="robot",
                scopes=["project:b"],
            )
        )
    proof = cap.prove(
        token,
        key=A_KEY,
        algorithm="hmac-sha256",
        audience="agent-a",
        tenant=org,
        request_digest="f" * 64,
        nonce="lookup",
        now=fp.now(),
    )
    authorization = io.authenticate(
        root,
        token,
        proof,
        request_digest="f" * 64,
        operation="read",
        purpose="support",
        principal=authenticated.key_prefix,
        tenant_id=org,
        consume=False,
    )
    root_token = delegation.ROOT.set(root)
    try:
        with io.activate(authorization):
            async with session_scope(session_factory, org_id=org) as session:
                assert await crud.get_trace(session, org, identity) is None
    finally:
        delegation.ROOT.reset(root_token)


async def test_native_dense_filter_excludes_private_classes_before_top_k(session_factory, config, tmp_path):
    org, _, root, token, _, authenticated = await setup(session_factory, config, tmp_path, classes=["fact"])
    scope = {"tenant": org, "labels": ["project:a"]}
    rows = [MemoryRecord(f"hidden-{i:03}", "episode", 1, {"text": "private wrist"}, scope=scope) for i in range(205)]
    rows.append(MemoryRecord("visible", "fact", 1, {"text": "wrist instruction"}, scope=scope))
    async with session_scope(session_factory, org_id=org) as session:
        await session.execute(
            insert(MemoryRecordProjection),
            [
                {"org_id": org, "id": record.id, "version": 1, "scope": scope, "record": record.to_dict()}
                for record in rows
            ],
        )

    class Vector:
        tenant = org
        seen = None

        async def search(self, vector, *, top_k, allowed_ids=None):
            self.seen = allowed_ids
            return [
                SimpleNamespace(key=record.id) for record in rows if allowed_ids is None or record.id in allowed_ids
            ][:top_k]

    vector = Vector()
    proof = cap.prove(
        token,
        key=A_KEY,
        algorithm="hmac-sha256",
        audience="agent-a",
        tenant=org,
        request_digest="f" * 64,
        nonce="dense",
        now=fp.now(),
    )
    authorization = io.authenticate(
        root,
        token,
        proof,
        request_digest="f" * 64,
        operation="read",
        purpose="support",
        principal=authenticated.key_prefix,
        tenant_id=org,
        consume=False,
    )
    root_token = delegation.ROOT.set(root)
    try:
        with io.activate(authorization):
            results = await PostgresJournal(session_factory, org, vector_index=vector).search(
                "no-lexical-match", scope={"labels": ["project:a"]}, limit=1, vector=[1.0]
            )
        assert vector.seen == {"visible"}
        assert [record.id for record in results] == ["visible"]
    finally:
        delegation.ROOT.reset(root_token)


@pytest.mark.parametrize("allow_episode_erasure", [False, True])
async def test_fact_erasure_cannot_launder_permission_to_erase_source_episode(
    session_factory, config, tmp_path, allow_episode_erasure
):
    classes = ["fact", "episode"] if allow_episode_erasure else ["fact"]
    org, raw_key, _, token, configured, _ = await setup(session_factory, config, tmp_path, classes=classes)
    scope = {"tenant": org, "labels": ["project:a"]}
    journal = PostgresJournal(session_factory, org)
    await journal.put(MemoryRecord("private-episode", "episode", 1, {"text": "Private source."}, scope=scope))
    record = MemoryRecord("citing-fact", "fact", 1,
                          {"text": "Derived content.", "source_message_ids": ["private-episode"]}, scope=scope)
    async with client(_app(session_factory, configured)) as app:
        target = "/v1/memory/record-put"
        raw = json.dumps({"record": record.to_dict(), "context": ["project:a"]}).encode()
        response = await app.post(target, headers=headers(raw_key, token, target, raw, "cite"), content=raw)
        assert response.status_code == 200, response.text
        target = "/v1/memory/record-forget"
        raw = json.dumps({"id": record.id, "reason": "Erase my fact.", "context": ["project:a"]}).encode()
        response = await app.post(target, headers=headers(raw_key, token, target, raw, "erase"), content=raw)
        assert response.status_code == (200 if allow_episode_erasure else 403), response.text
    async with session_scope(session_factory, org_id=org) as session:
        for identity in ("private-episode", "citing-fact"):
            row = await session.get(MemoryRecordProjection, (org, identity))
            assert (row is None) == allow_episode_erasure


async def test_native_mcp_http_handshake_and_tools_keep_primary_auth_and_leaf_scope(session_factory, config, tmp_path):
    from hub.server import build_app

    org, raw_key, _, token, configured, _ = await setup(session_factory, config, tmp_path)
    identity = "00000000-0000-0000-0000-000000000009"
    async with session_scope(session_factory, org_id=org) as session:
        session.add(
            Trace(
                id=identity,
                org_id=org,
                title="hidden",
                context_text="private",
                solution_text="private",
                agent_type="robot",
                scopes=["project:b"],
            )
        )
    app = build_app(replace(configured, allow_rls_bypass=True), session_factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8420"
        ) as remote:

            async def rpc(payload, nonce):
                raw = json.dumps(payload).encode()
                signed = headers(raw_key, token, "/mcp", raw, nonce)
                signed.pop("X-API-Key")
                signed.update(
                    {
                        "Authorization": "Bearer " + raw_key,
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    }
                )
                return await remote.post("/mcp", headers=signed, content=raw)

            response = await rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "unimem-capability-test", "version": "1"},
                    },
                },
                "init",
            )
            assert response.status_code == 200, response.text
            assert "mcp-session-id" not in response.headers
            assert "serverInfo" in response.json()["result"]
            response = await rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, "initialized")
            assert response.status_code in {200, 202}, response.text
            response = await rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "get_trace", "arguments": {"id": identity}},
                },
                "get",
            )
            assert response.status_code == 200, response.text
            assert "private" not in response.text
            response = await rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "account_usage", "arguments": {}},
                },
                "unsupported",
            )
            assert response.status_code == 403
    assert cap.CURRENT.get() is None and delegation.ROOT.get() == ""


async def test_capability_migration_forces_tenant_rls_as_non_bypass_reader(session_factory):
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
    schema, reader = "capability_migration_" + suffix, "capability_reader_" + suffix
    org, other = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await connection.execute(text("CREATE TABLE organizations(id UUID PRIMARY KEY)"))
            await connection.execute(
                text("INSERT INTO organizations VALUES (:org),(:other)"), {"org": org, "other": other}
            )
            await connection.run_sync(_migration, "fd25e7f9a1c3_capability_nonce_and_lineage_outbox.py")
            await connection.execute(text(f'CREATE ROLE "{reader}" NOSUPERUSER NOBYPASSRLS'))
            await connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{reader}"'))
            for table in ("capability_nonces", "capability_attestations"):
                await connection.execute(text(f'GRANT SELECT,INSERT,DELETE ON {table} TO "{reader}"'))
            await connection.execute(
                text(
                    "INSERT INTO capability_nonces VALUES (:org,repeat('a',64),now()+interval '30s'),"
                    "(:other,repeat('b',64),now()+interval '30s')"
                ),
                {"org": org, "other": other},
            )
            forced = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class WHERE relnamespace=current_schema()::regnamespace "
                    "AND relname IN ('capability_nonces','capability_attestations') "
                    "AND relrowsecurity AND relforcerowsecurity"
                )
            )
            assert forced == 2
            await connection.execute(text(f'SET LOCAL ROLE "{reader}"'))
            await connection.execute(text("SELECT set_config('app.org_id',:org,true)"), {"org": org})
            assert (await connection.execute(text("SELECT org_id::text FROM capability_nonces"))).scalars().all() == [
                org
            ]
            with pytest.raises(DBAPIError, match="row-level security"):
                async with connection.begin_nested():
                    await connection.execute(
                        text("INSERT INTO capability_nonces VALUES (:other,repeat('c',64),now())"), {"other": other}
                    )
            await connection.execute(text("SELECT set_config('app.org_id','',true)"))
            assert (await connection.execute(text("SELECT nonce_id FROM capability_nonces"))).all() == []
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await connection.execute(text(f'DROP ROLE IF EXISTS "{reader}"'))


async def test_leaf_write_permission_cannot_upgrade_read_only_primary_key(session_factory, config, tmp_path):
    org, raw_key, _, token, configured, _ = await setup(session_factory, config, tmp_path, primary_scopes=["read"])
    target = "/api/v1/traces"
    raw = b'{"title":"Denied","context_text":"wrist","solution_text":"inspect"}'
    async with client(_app(session_factory, configured)) as app:
        response = await app.post(target, headers=headers(raw_key, token, target, raw, "write"), content=raw)
        assert response.status_code == 403
    async with session_scope(session_factory, org_id=org) as session:
        assert not (await session.execute(select(Trace.id).where(Trace.org_id == org))).all()
