"""Data residency enforced at authentication: a deployment that declares a region does not serve an org pinned to
another one, and nothing changes for an org with no region or a deployment with none declared."""
import pytest

from hub import auth, manage
from hub.db import session_scope
from hub.models import Organization

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _restore_region():
    yield
    auth.configure_region("")                       # module-level setting: never leak into another test


async def _org_with_key(session_factory, region=None, name="Acme"):
    async with session_scope(session_factory) as session:
        org = Organization(name=name, data_region=region)
        session.add(org)
        await session.flush()
        issued = await auth.issue_api_key(session, org.id)
        return org.id, issued.raw_key


async def _verify(session_factory, raw_key):
    async with session_scope(session_factory) as session:
        return await auth.verify_api_key(session, raw_key)


@pytest.mark.parametrize("deployment,org,served", [
    ("us", "eu", False), ("eu", "eu", True), ("eu", None, True), ("", "eu", True), ("", None, True),
    ("EU", "eu", True), (" eu ", "EU", True), ("eu-west-1", "eu", False)])
async def test_who_a_deployment_serves(session_factory, config, deployment, org, served):
    _, key = await _org_with_key(session_factory, region=org)
    auth.configure_region(deployment)
    assert (await _verify(session_factory, key) is not None) is served


async def test_a_refusal_looks_like_a_wrong_key_and_nothing_about_regions_leaks_to_a_stranger(session_factory, config):
    _, key = await _org_with_key(session_factory, region="eu")
    auth.configure_region("us")
    refused = await _verify(session_factory, key)
    stranger = await _verify(session_factory, "ct_live_made-up")
    assert refused is None and stranger is None             # the same answer


async def test_the_legacy_scan_path_is_enforced_too(session_factory, config):
    """A key with no key_hmac yet goes through the Argon2 path; the region check must apply there as well."""
    from sqlalchemy import update

    from hub.models import ApiKey
    org_id, key = await _org_with_key(session_factory, region="eu")
    async with session_scope(session_factory) as session:
        await session.execute(update(ApiKey).where(ApiKey.org_id == org_id).values(key_hmac=None))
    auth.configure_region("us")
    assert await _verify(session_factory, key) is None
    auth.configure_region("eu")
    assert await _verify(session_factory, key) is not None


async def test_pinning_and_unpinning_takes_effect_on_the_next_request(session_factory, config):
    org_id, key = await _org_with_key(session_factory)
    auth.configure_region("us")
    assert await _verify(session_factory, key) is not None
    assert await manage.set_region(org_id, "eu", session_factory=session_factory)
    assert await _verify(session_factory, key) is None
    assert await manage.set_region(org_id, "none", session_factory=session_factory)
    assert await _verify(session_factory, key) is not None


@pytest.mark.parametrize("bad", ["EU WEST", "e/u", "-eu", "x" * 40, "é"])
async def test_a_malformed_region_is_refused(session_factory, config, bad, capsys):
    org_id, _ = await _org_with_key(session_factory)
    assert not await manage.set_region(org_id, bad, session_factory=session_factory)
    assert "letters, digits and hyphens" in capsys.readouterr().err


async def test_set_region_is_audited_and_refuses_an_unknown_org(session_factory, config, capsys):
    from sqlalchemy import select

    from hub.models import AuditLogEntry as AuditLog
    org_id, _ = await _org_with_key(session_factory)
    await manage.set_region(org_id, "eu", session_factory=session_factory)
    async with session_scope(session_factory) as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.action == "set_region"))).scalars().all()
    assert rows and "None -> 'eu'" in rows[0].summary
    assert not await manage.set_region("00000000-0000-0000-0000-000000000000", "eu", session_factory=session_factory)


async def test_the_disclosure_says_enforcement_is_on_only_when_a_region_is_declared(session_factory, config):
    import dataclasses

    import httpx
    from starlette.applications import Starlette

    from hub import disclosure
    for region, expected in (("eu", True), ("", False)):
        app = Starlette()
        disclosure.add_disclosure_route(app, dataclasses.replace(config, data_region=region))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            body = (await client.get("/disclosure")).json()
        assert body["region_enforced_for_pinned_orgs"] is expected
