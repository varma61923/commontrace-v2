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
    assert resolved is not None
    assert resolved.org_id == org_id
    # the non-secret prefix comes back for audit attribution, and is a
    # prefix of the raw key -- never the whole thing
    assert resolved.key_prefix == issued.raw_key[: len(resolved.key_prefix)]
    assert resolved.key_prefix != issued.raw_key


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


async def test_unknown_prefix_still_pays_the_argon2_cost(session_factory, config, monkeypatch):
    """A presented key whose prefix matches no row in the database used to
    return None immediately -- no argon2id verify() call at all -- while a
    key whose prefix DOES match a row (but the rest is wrong) always paid
    for a full verify(). That's a timing oracle: a remote attacker times
    responses to learn which key_prefix values exist without ever guessing
    a real key. verify_api_key must now burn the same verify() cost (against
    _DUMMY_HASH) on the no-candidate path too, so the two cases are not
    distinguishable by whether a hash was computed at all."""
    from argon2 import PasswordHasher

    calls = []
    real_verify = PasswordHasher.verify

    def _tracking_verify(self, hash_, key):
        calls.append(hash_)
        return real_verify(self, hash_, key)

    monkeypatch.setattr(PasswordHasher, "verify", _tracking_verify)

    async with session_scope(session_factory) as session:
        # well-formed prefix, but no ApiKey row has it at all
        resolved = await auth.verify_api_key(session, "ct_live_" + "z" * 40)
    assert resolved is None
    assert calls == [auth._DUMMY_HASH]


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
    assert new_resolves is not None
    assert new_resolves.org_id == org_id


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
