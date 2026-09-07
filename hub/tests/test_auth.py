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


async def test_a_corrupt_stored_hash_is_skipped_not_a_500(session_factory, config):
    """key_hash rows are assumed to always be well-formed argon2 hashes, but
    nothing enforces that at the DB layer -- corruption, a hand-edited row,
    or a hash written by code from a different scheme all produce a string
    argon2 can't parse. verify() then raises InvalidHashError, a ValueError
    subclass (not VerificationError), which the original `except
    VerifyMismatchError` did not catch -- it escaped verify_api_key
    entirely, turning one bad row into an unhandled exception (an HTTP 500
    via the auth middleware) instead of "this key doesn't match"."""
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
        resolved = await auth.verify_api_key(session, raw_key)  # must not raise
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


# --- expiry --------------------------------------------------------------


async def test_expires_days_must_be_positive(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        with pytest.raises(ValueError):
            await auth.issue_api_key(session, org_id, expires_days=0)
        with pytest.raises(ValueError):
            await auth.issue_api_key(session, org_id, expires_days=-5)


async def test_non_expiring_key_by_default(session_factory, config):
    """expires_days=None (the default, unchanged) issues a key that keeps
    verifying indefinitely -- expires_at stays NULL, not some far-future
    sentinel a caller could accidentally compare against."""
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
    assert row.expires_at is None

    async with session_scope(session_factory) as session:
        resolved = await auth.verify_api_key(session, issued.raw_key)
    assert resolved is not None


async def test_expired_key_no_longer_verifies(session_factory, config):
    """verify_api_key's `candidate.expires_at <= now` check, exercised
    against a real expired row rather than trusted from the comment above
    it -- an expired key must read exactly like an invalid one (None, not
    an exception, and no distinguishing detail)."""
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
    """rotate_api_key's docstring claims a key issued to expire in N days
    rotates into another ~N-day key rather than silently becoming
    non-expiring. Checked against the actual computed expires_at, with a
    day of slack for the round-trip through total_seconds()/86400."""
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        original = await auth.issue_api_key(session, org_id, expires_days=90)

    async with session_scope(session_factory) as session:
        rotated = await auth.rotate_api_key(session, original.key_id)
        row = (await session.execute(select(ApiKey).where(ApiKey.id == rotated.key_id))).scalar_one()

    assert row.expires_at is not None
    expected = datetime.now(timezone.utc) + timedelta(days=90)
    assert abs((row.expires_at - expected).total_seconds()) < 86400


# --- last_used_at throttling ----------------------------------------------


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
    """Verified against the actual DB row, not just "no exception": a hot
    key must not pay for an UPDATE on every single authenticated request
    (see auth.py's write-throttling rationale) -- a second verification a
    moment later must leave the timestamp exactly as the first call set
    it."""
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


# --- prefix collisions -----------------------------------------------------


async def test_two_keys_sharing_a_prefix_are_both_individually_reachable(session_factory, config):
    """key_prefix is only the first 12 chars of the raw key, so two issued
    keys can (rarely, but for real, since it's a random suffix) share one --
    verify_api_key's candidate loop must try every row with that prefix, not
    just the first one the SELECT happens to return, or a live prefix
    collision would make an org's real key stop authenticating the moment
    it collided with someone else's.

    Constructs the collision directly (an ApiKey row whose key_prefix and
    key_hash both genuinely correspond to a second, distinct raw key) rather
    than just editing the prefix column, so this exercises the real
    multiple-candidates-per-prefix path, not a row that no longer matches
    its own supposed raw key."""
    org_id_a = await _make_org(session_factory)
    org_id_b = await _make_org(session_factory)

    async with session_scope(session_factory) as session:
        issued_a = await auth.issue_api_key(session, org_id_a)

    prefix = issued_a.raw_key[: auth._PREFIX_LEN]
    raw_key_b = prefix + "distinct-suffix-" + "z" * 24  # same prefix, different secret
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


# --- revocation race window -------------------------------------------------


async def test_revocation_landing_during_a_slow_verify_still_denies_it(session_factory, config, monkeypatch):
    """auth.py's comment on the fresh-revocation-reread describes an
    operator's revoke_api_key landing WHILE this request's slow argon2
    verify() is still in flight -- the initial `revoked_at IS NULL` SELECT
    already passed, and the in-memory `candidate` has no way to see a commit
    that happens after it was loaded, so verify_api_key re-reads revoked_at
    fresh, after verify() returns, rather than trusting `candidate`.

    This is a property of the LEGACY (Argon2 prefix-scan) path specifically:
    the fast `key_hmac` path has no such window to close in the first place
    -- revocation is read in the SAME single indexed SELECT that finds the
    row, with no slow operation in between for a concurrent revoke to race
    against. A freshly issued key now has `key_hmac` set at issuance
    (auth.issue_api_key), so it would resolve via the fast path and never
    reach Argon2 at all -- clear it here to force this specific request
    through the legacy path this test is actually about, exactly as a key
    issued before the key_hmac column existed would.

    Exercised for real rather than trusted from the comment: verify() is
    blocked with a real threading.Event while running on its actual
    to_thread worker thread (the same offloading production uses), the key
    is revoked from a separate session while it's stuck there, and only
    then released -- so this proves the recheck reads the committed
    revocation, not just that revoking before starting a fresh verify call
    denies it (test_revoked_key_no_longer_verifies already covers that
    simpler, non-overlapping case)."""
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

    # The key is revoked here, from a separate session, while verify_task is
    # still parked inside the (mocked) argon2 call above -- the exact window
    # the fresh reread exists to close.
    async with session_scope(session_factory) as session:
        await auth.revoke_api_key(session, issued.key_id)

    release_verify.set()
    resolved = await verify_task
    assert resolved is None, "revocation landing mid-verify() was not honored -- the race window is open"


# --- key_hmac fast path ----------------------------------------------------
#
# hub/auth.py's verify_api_key tries an indexed key_hmac lookup before
# falling back to the Argon2 prefix-scan tested above. Measured motivation:
# Argon2id verify() costs ~83ms/64MiB per call on a 4-core box, a ~48 req/s
# ceiling on every authenticated request the Hub serves. These tests pin the
# fast path's own correctness and its backward-compatible interaction with
# the legacy path it sits in front of.


async def test_a_freshly_issued_key_never_calls_argon2_verify(session_factory, config, monkeypatch):
    """issue_api_key now writes key_hmac at issuance, so a normal
    verification of a freshly issued key should resolve entirely through the
    fast indexed lookup -- zero Argon2 computations, not merely a faster one.
    This is the throughput claim itself, pinned as a test rather than left
    to a benchmark someone might stop running."""
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
    """Simulates a key issued before the key_hmac column existed: no
    key_hmac set at all. The first verification must still succeed (via the
    legacy path) AND leave key_hmac populated, so every verification after
    that one takes the fast path -- zero further Argon2 calls."""
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
    """The fast path has its own revocation check -- this proves it, not
    just the legacy path's (test_revoked_key_no_longer_verifies)."""
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
    assert row.last_used_at == first_seen  # unchanged: within the throttle interval


async def test_key_hmac_is_never_the_raw_key_or_the_argon2_hash(session_factory, config):
    org_id = await _make_org(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)

    async with session_scope(session_factory) as session:
        row = (await session.execute(select(ApiKey).where(ApiKey.id == issued.key_id))).scalar_one()
    assert issued.raw_key not in row.key_hmac
    assert row.key_hmac != row.key_hash
    assert len(row.key_hmac) == 64  # hex sha256
