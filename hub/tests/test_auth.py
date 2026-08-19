from __future__ import annotations

import pytest

from hub import auth
from hub.db import session_scope
from hub.models import Organization

pytestmark = pytest.mark.asyncio


async def _make_org(session_factory) -> str:
    async with session_scope(session_factory) as session:
        org = Organization(name="test-org")
        session.add(org)
        await session.flush()
        return org.id


async def test_issued_key_verifies_and_resolves_to_org(session_factory, config):
    org_id = await _make_org(session_factory)

    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)

    async with session_scope(session_factory) as session:
        resolved = await auth.verify_api_key(session, issued.raw_key)
    assert resolved == org_id


async def test_wrong_key_does_not_verify(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        await auth.issue_api_key(session, org_id)

    async with session_scope(session_factory) as session:
        resolved = await auth.verify_api_key(session, "ct_live_totally-made-up-key")
    assert resolved is None


async def test_malformed_key_does_not_verify(session_factory, config):
    async with session_scope(session_factory) as session:
        resolved = await auth.verify_api_key(session, "not-even-the-right-prefix")
    assert resolved is None


async def test_revoked_key_no_longer_verifies(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)

    async with session_scope(session_factory) as session:
        await auth.revoke_api_key(session, issued.key_id)

    async with session_scope(session_factory) as session:
        resolved = await auth.verify_api_key(session, issued.raw_key)
    assert resolved is None


async def test_rotate_key_revokes_old_and_issues_new(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        original = await auth.issue_api_key(session, org_id)

    async with session_scope(session_factory) as session:
        rotated = await auth.rotate_api_key(session, original.key_id)

    assert rotated.raw_key != original.raw_key
    assert rotated.org_id == org_id

    async with session_scope(session_factory) as session:
        old_resolves = await auth.verify_api_key(session, original.raw_key)
        new_resolves = await auth.verify_api_key(session, rotated.raw_key)
    assert old_resolves is None
    assert new_resolves == org_id


async def test_raw_key_is_never_persisted_verbatim(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)

    from sqlalchemy import select

    from hub.models import ApiKey

    async with session_scope(session_factory) as session:
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
    assert issued.raw_key not in row.key_hash
    assert row.key_hash.startswith("$argon2")
