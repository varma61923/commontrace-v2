from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from hub import auth
from hub.db import session_scope
from hub.models import ApiKey, Organization

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
    from argon2 import PasswordHasher

    calls = []
    real_verify = PasswordHasher.verify

    def _tracking_verify(self, hash_, key):
        calls.append(hash_)
        return real_verify(self, hash_, key)

    monkeypatch.setattr(PasswordHasher, "verify", _tracking_verify)

    async with session_scope(session_factory) as session:
        resolved = await auth.verify_api_key(session, "ct_live_" + "z" * 40)
    assert resolved is None
    assert calls == [auth._DUMMY_HASH]


async def test_a_corrupt_stored_hash_is_skipped_not_a_500(session_factory, config):
    from hub.models import ApiKey

    org_id = await _make_org(session_factory)
    raw_key = auth.generate_raw_key()
    async with session_scope(session_factory) as session:
        session.add(
            ApiKey(
                org_id=org_id,
                key_prefix=raw_key[: auth._PREFIX_LEN],
                key_hash="not-a-valid-argon2-hash-at-all",
            )
        )

    async with session_scope(session_factory) as session:
        resolved = await auth.verify_api_key(session, raw_key)
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
    assert new_resolves is not None
    assert new_resolves.org_id == org_id


async def test_raw_key_is_never_persisted_verbatim(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)

    async with session_scope(session_factory) as session:
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
    assert issued.raw_key not in row.key_hash
    assert row.key_hash.startswith("$argon2")


async def test_expires_days_must_be_positive(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        with pytest.raises(ValueError):
            await auth.issue_api_key(session, org_id, expires_days=0)
        with pytest.raises(ValueError):
            await auth.issue_api_key(session, org_id, expires_days=-5)


async def test_non_expiring_key_by_default(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
    assert row.expires_at is None

    async with session_scope(session_factory) as session:
        resolved = await auth.verify_api_key(session, issued.raw_key)
    assert resolved is not None


async def test_expired_key_no_longer_verifies(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id, expires_days=1)

    async with session_scope(session_factory) as session:
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    async with session_scope(session_factory) as session:
        resolved = await auth.verify_api_key(session, issued.raw_key)
    assert resolved is None


async def test_key_expiring_in_the_future_still_verifies(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id, expires_days=30)

    async with session_scope(session_factory) as session:
        resolved = await auth.verify_api_key(session, issued.raw_key)
    assert resolved is not None
    assert resolved.org_id == org_id


async def test_rotate_key_carries_forward_the_expiry_policy(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        original = await auth.issue_api_key(session, org_id, expires_days=90)

    async with session_scope(session_factory) as session:
        rotated = await auth.rotate_api_key(session, original.key_id)
        row = (await session.execute(select(ApiKey).where(ApiKey.id == rotated.key_id))).scalar_one()

    assert row.expires_at is not None
    expected = datetime.now(timezone.utc) + timedelta(days=90)
    assert abs((row.expires_at - expected).total_seconds()) < 86400


async def test_rotating_a_narrow_key_does_not_widen_it(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        original = await auth.issue_api_key(session, org_id, scopes=["read"])
    async with session_scope(session_factory) as session:
        rotated = await auth.rotate_api_key(session, original.key_id)
        row = (await session.execute(select(ApiKey).where(ApiKey.id == rotated.key_id))).scalar_one()
    assert list(row.scopes) == ["read"]
    assert tuple(rotated.scopes) == ("read",)


async def test_a_revoked_key_cannot_be_rotated_back_to_life(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        original = await auth.issue_api_key(session, org_id)
    async with session_scope(session_factory) as session:
        await auth.revoke_api_key(session, original.key_id)
    with pytest.raises(ValueError, match="revoked"):
        async with session_scope(session_factory) as session:
            await auth.rotate_api_key(session, original.key_id)
    async with session_scope(session_factory) as session:
        live = (await session.execute(
            select(ApiKey).where(ApiKey.org_id == org_id, ApiKey.revoked_at.is_(None))
        )).scalars().all()
    assert live == []


async def test_concurrent_rotations_of_one_key_mint_exactly_one_successor(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        original = await auth.issue_api_key(session, org_id)

    async def rotate():
        try:
            async with session_scope(session_factory) as session:
                return await auth.rotate_api_key(session, original.key_id)
        except ValueError:
            return None

    results = await asyncio.gather(*[rotate() for _ in range(6)])
    assert len([r for r in results if r is not None]) == 1
    async with session_scope(session_factory) as session:
        live = (await session.execute(
            select(ApiKey).where(ApiKey.org_id == org_id, ApiKey.revoked_at.is_(None))
        )).scalars().all()
    assert len(live) == 1


async def test_last_used_at_is_set_on_first_verification(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
    assert row.last_used_at is None

    async with session_scope(session_factory) as session:
        await auth.verify_api_key(session, issued.raw_key)

    async with session_scope(session_factory) as session:
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
    assert row.last_used_at is not None


async def test_last_used_at_is_not_rewritten_within_the_throttle_interval(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)

    async with session_scope(session_factory) as session:
        await auth.verify_api_key(session, issued.raw_key)
    async with session_scope(session_factory) as session:
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
        first_seen = row.last_used_at

    async with session_scope(session_factory) as session:
        await auth.verify_api_key(session, issued.raw_key)
    async with session_scope(session_factory) as session:
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
    assert row.last_used_at == first_seen


async def test_last_used_at_is_refreshed_after_the_throttle_interval(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)

    async with session_scope(session_factory) as session:
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
        stale = datetime.now(timezone.utc) - auth._LAST_USED_AT_UPDATE_INTERVAL - timedelta(seconds=1)
        row.last_used_at = stale

    async with session_scope(session_factory) as session:
        await auth.verify_api_key(session, issued.raw_key)

    async with session_scope(session_factory) as session:
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
    assert row.last_used_at > stale


async def test_two_keys_sharing_a_prefix_are_both_individually_reachable(session_factory, config):
    org_id_a = await _make_org(session_factory)
    org_id_b = await _make_org(session_factory)

    async with session_scope(session_factory) as session:
        issued_a = await auth.issue_api_key(session, org_id_a)

    prefix = issued_a.raw_key[: auth._PREFIX_LEN]
    raw_key_b = prefix + "distinct-suffix-" + "z" * 24
    key_hash_b = auth._hasher.hash(raw_key_b)

    async with session_scope(session_factory) as session:
        session.add(ApiKey(org_id=org_id_b, key_prefix=prefix, key_hash=key_hash_b))

    async with session_scope(session_factory) as session:
        candidates = (
            await session.execute(select(ApiKey).where(ApiKey.key_prefix == prefix))
        ).scalars().all()
    assert len(candidates) == 2, "test setup didn't actually create a prefix collision"

    async with session_scope(session_factory) as session:
        resolved_a = await auth.verify_api_key(session, issued_a.raw_key)
        resolved_b = await auth.verify_api_key(session, raw_key_b)

    assert resolved_a is not None and resolved_a.org_id == org_id_a
    assert resolved_b is not None and resolved_b.org_id == org_id_b


async def test_revocation_landing_during_a_slow_verify_still_denies_it(session_factory, config, monkeypatch):
    from argon2 import PasswordHasher

    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)
    async with session_scope(session_factory) as session:
        await session.execute(
            update(ApiKey).where(ApiKey.id == issued.key_id).values(key_hmac=None)
        )

    verify_started = threading.Event()
    release_verify = threading.Event()
    real_verify = PasswordHasher.verify

    def _blocking_verify(self, hash_, key):
        verify_started.set()
        assert release_verify.wait(timeout=5), "test setup never released verify() -- test itself is broken"
        return real_verify(self, hash_, key)

    monkeypatch.setattr(PasswordHasher, "verify", _blocking_verify)

    async def _do_verify():
        async with session_scope(session_factory) as session:
            return await auth.verify_api_key(session, issued.raw_key)

    verify_task = asyncio.ensure_future(_do_verify())
    assert await asyncio.to_thread(verify_started.wait, 5), "verify() never reached the blocking point"

    async with session_scope(session_factory) as session:
        await auth.revoke_api_key(session, issued.key_id)

    release_verify.set()
    resolved = await verify_task
    assert resolved is None, "revocation landing mid-verify() was not honored -- the race window is open"


async def test_a_freshly_issued_key_never_calls_argon2_verify(session_factory, config, monkeypatch):
    from argon2 import PasswordHasher

    calls = []
    real_verify = PasswordHasher.verify

    def _tracking_verify(self, hash_, key):
        calls.append(hash_)
        return real_verify(self, hash_, key)

    monkeypatch.setattr(PasswordHasher, "verify", _tracking_verify)

    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)

    async with session_scope(session_factory) as session:
        resolved = await auth.verify_api_key(session, issued.raw_key)

    assert resolved is not None
    assert resolved.org_id == org_id
    assert calls == []


async def test_a_pre_hmac_key_is_backfilled_on_first_legacy_verification(session_factory, config, monkeypatch):
    from argon2 import PasswordHasher

    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)
    async with session_scope(session_factory) as session:
        await session.execute(
            update(ApiKey).where(ApiKey.id == issued.key_id).values(key_hmac=None)
        )

    async with session_scope(session_factory) as session:
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
    assert row.key_hmac is None

    async with session_scope(session_factory) as session:
        resolved = await auth.verify_api_key(session, issued.raw_key)
    assert resolved is not None

    async with session_scope(session_factory) as session:
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
    assert row.key_hmac is not None
    assert row.key_hmac == auth._key_hmac(issued.raw_key)

    calls = []
    real_verify = PasswordHasher.verify

    def _tracking_verify(self, hash_, key):
        calls.append(hash_)
        return real_verify(self, hash_, key)

    monkeypatch.setattr(PasswordHasher, "verify", _tracking_verify)

    async with session_scope(session_factory) as session:
        resolved2 = await auth.verify_api_key(session, issued.raw_key)
    assert resolved2 is not None
    assert calls == []


async def test_revoked_key_does_not_verify_via_the_fast_path(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)
    async with session_scope(session_factory) as session:
        await auth.revoke_api_key(session, issued.key_id)

    async with session_scope(session_factory) as session:
        resolved = await auth.verify_api_key(session, issued.raw_key)
    assert resolved is None


async def test_expired_key_does_not_verify_via_the_fast_path(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id, expires_days=1)
    async with session_scope(session_factory) as session:
        await session.execute(
            update(ApiKey).where(ApiKey.id == issued.key_id)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        )

    async with session_scope(session_factory) as session:
        resolved = await auth.verify_api_key(session, issued.raw_key)
    assert resolved is None


async def test_last_used_at_throttling_applies_on_the_fast_path_too(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)

    async with session_scope(session_factory) as session:
        await auth.verify_api_key(session, issued.raw_key)
    async with session_scope(session_factory) as session:
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
    first_seen = row.last_used_at
    assert first_seen is not None

    async with session_scope(session_factory) as session:
        await auth.verify_api_key(session, issued.raw_key)
    async with session_scope(session_factory) as session:
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
    assert row.last_used_at == first_seen


async def test_key_hmac_is_never_the_raw_key_or_the_argon2_hash(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)

    async with session_scope(session_factory) as session:
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
    assert issued.raw_key not in row.key_hmac
    assert row.key_hmac != row.key_hash
    assert len(row.key_hmac) == 64


class TestPepperCanComeFromAFile:
    @pytest.fixture
    def reload_auth(self, monkeypatch, tmp_path):
        import importlib

        yield tmp_path
        monkeypatch.delenv("HUB_API_KEY_PEPPER", raising=False)
        monkeypatch.delenv("HUB_API_KEY_PEPPER_FILE", raising=False)
        importlib.reload(auth)

    async def test_pepper_file_is_read_into_the_module_level_pepper(self, monkeypatch, reload_auth):
        import importlib

        pepper_file = reload_auth / "pepper"
        pepper_file.write_text("a-fixed-pepper-for-this-test\n")
        monkeypatch.delenv("HUB_API_KEY_PEPPER", raising=False)
        monkeypatch.setenv("HUB_API_KEY_PEPPER_FILE", str(pepper_file))

        importlib.reload(auth)
        assert auth._pepper_env == "a-fixed-pepper-for-this-test"
        assert auth._PEPPER == b"a-fixed-pepper-for-this-test"

    async def test_pepper_file_wins_over_a_plain_env_var_set_alongside_it(
        self, monkeypatch, reload_auth
    ):
        import importlib

        pepper_file = reload_auth / "pepper"
        pepper_file.write_text("from-the-secret-store")
        monkeypatch.setenv("HUB_API_KEY_PEPPER", "stale-plaintext-pepper")
        monkeypatch.setenv("HUB_API_KEY_PEPPER_FILE", str(pepper_file))

        importlib.reload(auth)
        assert auth._pepper_env == "from-the-secret-store"
