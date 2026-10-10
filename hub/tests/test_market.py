from __future__ import annotations

import dataclasses
import json
import os

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from starlette.applications import Starlette

from commontrace import lesson_io, marketplace, paths, templates
from hub import auth, rest
from hub.abuse import make_rate_limiter
from hub.alembic.versions.b8e3f1a2c7d4_lesson_marketplace import _OWN_ROWS, _READ, _UNSCOPED
from hub.db import session_scope
from hub.models import Organization
from hub.tests import test_row_level_security as _rls

pytestmark = pytest.mark.asyncio


def _lesson(root):
    os.makedirs(paths.lessons_dir(root), exist_ok=True)
    fm = templates.lesson_frontmatter(
        slug="retry-flaky-network", description="Retry idempotent network calls with backoff",
        agent_type="coder", domain="networking", tags=["network"], applies_when="transient failure",
        do_not_apply_when="the call mutates state", importance=4, importance_rationale="saves reruns",
        source_traces=[], status="active")
    path = os.path.join(paths.lessons_dir(root), "lesson_retry-flaky-network.md")
    lesson_io.write_lesson(path, fm, "## Rule\nRetry with backoff.\n", root=root, actor="t", reason="t")


@pytest.fixture
def signed(tmp_path):
    fleet_a, fleet_b, referee = (str(tmp_path / n) for n in ("a", "b", "ref"))
    for root in (fleet_a, fleet_b):
        _lesson(root)
    marketplace.identity(fleet_a, "fleet", principal_id="fleet-a", organization="acme", create=True)
    marketplace.identity(fleet_b, "fleet", principal_id="fleet-b", organization="globex", create=True)
    ref = marketplace.identity(referee, "referee", principal_id="ref", organization="auditor", create=True)
    pub = marketplace.identity(fleet_a, "publisher", principal_id="acme-pub", organization="acme", create=True)
    for root in (fleet_a, fleet_b):
        marketplace.trust(referee, marketplace.card(marketplace.identity(root, "fleet")), role="fleet")
    receipts = [marketplace.attest(fleet_a, "retry-flaky-network", effect={"effect": .2, "standard_error": .04}),
                marketplace.attest(fleet_b, "retry-flaky-network", effect={"effect": .15, "standard_error": .05})]
    listing = marketplace.publish(fleet_a, "retry-flaky-network", marketplace.certify(referee, receipts),
                                  {"id": "MIT", "terms": "MIT licence", "price": {"amount_usd": 5, "per": "install"}})
    return {"listing": listing, "publisher": marketplace.card(pub), "referee": marketplace.card(ref)}


async def _org(session_factory, name):
    async with session_scope(session_factory) as session:
        org = Organization(name=name)
        session.add(org)
        await session.flush()
        issued = await auth.issue_api_key(session, org.id)
        return {"X-API-Key": issued.raw_key}


def _client(session_factory, config, referee=None):
    if referee:
        config = dataclasses.replace(config, market_referees=((referee["id"], referee["organization"],
                                                               referee["public_key"]),))
    app = Starlette()
    rest.add_rest_routes(app, session_factory, config=config, rate_limiter=make_rate_limiter(config))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_upload_verify_browse_and_withdraw(session_factory, config, signed):
    acme, other = await _org(session_factory, "Acme"), await _org(session_factory, "Other")
    async with _client(session_factory, config, signed["referee"]) as client:
        listing = {"listing": signed["listing"]}
        r = await client.post("/api/v1/market/listings", json=listing, headers=acme)
        assert r.status_code == 403  # publisher key not registered yet
        r = await client.post("/api/v1/market/publishers", json={"card": signed["publisher"]}, headers=acme)
        assert r.status_code == 201
        r = await client.post("/api/v1/market/publishers", json={"card": signed["publisher"]}, headers=other)
        assert r.status_code == 409  # another org cannot claim the same publisher id
        r = await client.post("/api/v1/market/listings", json=listing, headers=other)
        assert r.status_code == 403  # the key belongs to Acme, not to Other
        r = await client.post("/api/v1/market/listings", json=listing, headers=acme)
        assert r.status_code == 201, r.text
        lid = r.json()["id"]
        assert (await client.post("/api/v1/market/listings", json=listing, headers=acme)).status_code == 200

        found = (await client.get("/api/v1/market/listings?q=retry backoff", headers=other)).json()["listings"]
        assert [x["id"] for x in found] == [lid]
        assert found[0]["organizations"] == 2 and found[0]["lift_ci"][0] > 0 and found[0]["price"]["amount_usd"] == 5
        full = (await client.get(f"/api/v1/market/listings/{lid}", headers=other)).json()["listing"]
        trusted = {"publisher": {"acme-pub": marketplace._principal_from_card(signed["publisher"], "publisher")},
                   "referee": {"ref": marketplace._principal_from_card(signed["referee"], "referee")},
                   "fleet": {}}
        assert marketplace.verify(full, trusted)["ok"]  # what the buyer downloads still verifies locally
        card = (await client.get("/api/v1/market/publishers/acme-pub", headers=other)).json()
        assert card["public_key"] == signed["publisher"]["public_key"]

        assert (await client.delete(f"/api/v1/market/listings/{lid}", headers=other)).status_code == 404
        r = await client.delete(f"/api/v1/market/listings/{lid}", headers=acme)
        assert r.json()["status"] == "withdrawn"
        assert (await client.get("/api/v1/market/listings", headers=other)).json()["listings"] == []
        assert (await client.get(f"/api/v1/market/listings/{lid}", headers=other)).status_code == 404


async def test_tampered_or_unrefereed_listings_are_refused(session_factory, config, signed):
    acme = await _org(session_factory, "Acme")
    async with _client(session_factory, config) as client:
        await client.post("/api/v1/market/publishers", json={"card": signed["publisher"]}, headers=acme)
        r = await client.post("/api/v1/market/listings", json={"listing": signed["listing"]}, headers=acme)
        assert r.status_code == 503  # no referee configured: nothing can be certified
    async with _client(session_factory, config, signed["referee"]) as client:
        tampered = json.loads(json.dumps(signed["listing"]))
        tampered["record"]["certificate"]["record"]["pooled"]["ci_low"] = 0.5
        r = await client.post("/api/v1/market/listings", json={"listing": tampered}, headers=acme)
        assert r.status_code == 422 and "signature" in r.json()["detail"]


# An unprivileged role, so the policies bite even when tests connect as a superuser.
enforcing_factory = _rls.enforcing_factory


@pytest_asyncio.fixture
async def market_rls(session_factory):
    async with session_scope(session_factory) as session:
        for table, read in _READ.items():
            await session.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            await session.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
            await session.execute(text(
                f"CREATE POLICY org_isolation ON {table} AS PERMISSIVE FOR ALL "
                f"USING ({_UNSCOPED} OR {_OWN_ROWS}) WITH CHECK ({_UNSCOPED} OR {_OWN_ROWS})"))
            await session.execute(text(
                f"CREATE POLICY market_public_read ON {table} AS PERMISSIVE FOR SELECT USING ({read})"))
    try:
        yield
    finally:
        async with session_scope(session_factory) as session:
            for table in _READ:
                await session.execute(text(f"DROP POLICY IF EXISTS market_public_read ON {table}"))
                await session.execute(text(f"DROP POLICY IF EXISTS org_isolation ON {table}"))
                await session.execute(text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
                await session.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))


async def test_rls_lets_every_org_read_the_catalog_but_never_write_anothers_rows(
        session_factory, market_rls, enforcing_factory):
    async with session_scope(session_factory) as session:
        if enforcing_factory is not session_factory:
            for table in _READ:
                await session.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_rls._RLS_ROLE}"))
        a, b = Organization(name="mkt-a"), Organization(name="mkt-b")
        session.add_all([a, b])
        await session.flush()
        a_id, b_id = str(a.id), str(b.id)
        await session.execute(text(
            "INSERT INTO market_publishers (id, org_id, principal_id, organization, public_key, created_at) "
            "VALUES (gen_random_uuid(), :o, 'pub-b', 'b', :k, now())"), {"o": b_id, "k": "A" * 44})
    token = auth.current_org_id.set(a_id)
    try:
        async with session_scope(enforcing_factory) as session:
            seen = (await session.execute(text(
                "SELECT principal_id FROM market_publishers WHERE principal_id = 'pub-b'"))).all()
            assert len(seen) == 1, "the public catalog must be readable across orgs"
            deleted = await session.execute(text("DELETE FROM market_publishers WHERE principal_id = 'pub-b'"))
            assert deleted.rowcount == 0, "another org deleted a publisher it can only read"
        try:
            async with session_scope(enforcing_factory) as session:
                moved = await session.execute(text(
                    "UPDATE market_publishers SET org_id = :a WHERE principal_id = 'pub-b'"), {"a": a_id})
                assert moved.rowcount == 0
        except Exception as exc:  # noqa: BLE001
            assert "row-level security" in str(exc).lower()
    finally:
        auth.current_org_id.reset(token)
