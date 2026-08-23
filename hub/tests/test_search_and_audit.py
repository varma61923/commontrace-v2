"""Tests for the production-hardening pass: search pagination + full-text
matching, the N+1 batch-loading fix, audit-log writes, and API-key expiry."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from hub import audit, auth, crud
from hub.abuse import TraceRejected, make_rate_limiter
from hub.config import MAX_SEARCH_LIMIT
from hub.db import session_scope
from hub.models import ApiKey, AuditLogEntry, Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="search-org")
        session.add(o)
        await session.flush()
        return o.id


@pytest_asyncio.fixture
async def other_org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="other-search-org")
        session.add(o)
        await session.flush()
        return o.id


async def _contribute(session_factory, config, org_id, title, context, solution, tags=None, actor="test"):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        return await crud.contribute_trace(
            session, org_id, config, rate_limiter,
            title=title, context_text=context, solution_text=solution,
            tags=tags or [], agent_type="code", actor=actor,
        )


class TestPagination:
    async def test_paginates_past_the_old_hard_cap(self, session_factory, config, org):
        for i in range(7):
            await _contribute(session_factory, config, org, f"trace {i}", f"context {i}", f"solution {i}")

        async with session_scope(session_factory) as session:
            page1 = await crud.search_traces(session, org, limit=3, offset=0)
        assert len(page1["traces"]) == 3
        assert page1["has_more"] is True
        assert page1["limit"] == 3 and page1["offset"] == 0

        async with session_scope(session_factory) as session:
            page3 = await crud.search_traces(session, org, limit=3, offset=6)
        assert len(page3["traces"]) == 1
        assert page3["has_more"] is False

    async def test_pages_do_not_overlap(self, session_factory, config, org):
        for i in range(6):
            await _contribute(session_factory, config, org, f"trace {i}", f"context {i}", f"solution {i}")
        async with session_scope(session_factory) as session:
            a = await crud.search_traces(session, org, limit=3, offset=0)
            b = await crud.search_traces(session, org, limit=3, offset=3)
        ids_a = {t["id"] for t in a["traces"]}
        ids_b = {t["id"] for t in b["traces"]}
        assert ids_a and ids_b
        assert ids_a.isdisjoint(ids_b)

    async def test_limit_is_clamped_to_max(self, session_factory, config, org):
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, limit=10_000)
        assert page["limit"] == MAX_SEARCH_LIMIT

    async def test_nonsense_limit_and_offset_are_coerced_not_crashing(self, session_factory, config, org):
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, limit=0, offset=-5)
        assert page["limit"] == 1
        assert page["offset"] == 0


class TestFullTextSearch:
    async def test_matches_on_word(self, session_factory, config, org):
        await _contribute(
            session_factory, config, org,
            "Kubernetes rollout stalled", "the readiness probe timed out", "raised the probe threshold",
        )
        await _contribute(session_factory, config, org, "Billing question", "invoice query", "explained line items")

        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, query="readiness probe")
        assert len(page["traces"]) == 1
        assert "Kubernetes" in page["traces"][0]["title"]

    async def test_stemming_matches_word_variants(self, session_factory, config, org):
        """A capability the old ILIKE substring match did NOT have: querying
        'deploy' finds a trace that says 'deployed'."""
        await _contribute(
            session_factory, config, org, "Release notes", "we deployed on friday", "rolled back safely"
        )
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, query="deploy")
        assert len(page["traces"]) == 1

    async def test_query_with_punctuation_does_not_error(self, session_factory, config, org):
        """plainto_tsquery must swallow operator characters that would make
        to_tsquery raise a syntax error on user-supplied input."""
        await _contribute(session_factory, config, org, "t", "some context", "some solution")
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, query="!!! & | context ???")
        assert isinstance(page["traces"], list)

    async def test_empty_query_returns_recent_traces(self, session_factory, config, org):
        await _contribute(session_factory, config, org, "t1", "c1", "s1")
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, query="")
        assert len(page["traces"]) == 1


class TestBatchHydration:
    async def test_votes_and_relations_attach_to_the_right_traces(self, session_factory, config, org):
        """Guards the N+1 fix: batch-loading must not cross-wire one trace's
        votes onto another's."""
        a = await _contribute(session_factory, config, org, "alpha", "ca", "sa")
        b = await _contribute(session_factory, config, org, "bravo", "cb", "sb")

        async with session_scope(session_factory) as session:
            await crud.vote_trace(session, org, a["id"], "up", feedback_text="only on alpha")

        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org)
        by_id = {t["id"]: t for t in page["traces"]}
        assert len(by_id[a["id"]]["votes"]) == 1
        assert by_id[a["id"]]["votes"][0]["feedback_text"] == "only on alpha"
        assert by_id[b["id"]]["votes"] == []


class TestAuditLog:
    async def test_contribute_writes_an_audit_row(self, session_factory, config, org):
        result = await _contribute(
            session_factory, config, org, "audited", "ctx", "sol", actor="api-key:ct_live_abcd"
        )
        async with session_scope(session_factory) as session:
            rows = (await session.execute(select(AuditLogEntry))).scalars().all()
        assert len(rows) == 1
        assert rows[0].action == "contribute_trace"
        assert rows[0].actor == "api-key:ct_live_abcd"
        assert rows[0].org_id == org
        assert rows[0].target_id == result["id"]

    async def test_audit_summary_never_contains_trace_content(self, session_factory, config, org):
        secret = "CUSTOMER-SECRET-PAYLOAD"
        await _contribute(session_factory, config, org, secret, secret, secret)
        async with session_scope(session_factory) as session:
            rows = (await session.execute(select(AuditLogEntry))).scalars().all()
        blob = " ".join(f"{r.summary} {r.target_type} {r.action}" for r in rows)
        assert secret not in blob

    async def test_vote_and_amend_are_audited(self, session_factory, config, org):
        trace = await _contribute(session_factory, config, org, "t", "c", "s")
        async with session_scope(session_factory) as session:
            await crud.vote_trace(session, org, trace["id"], "up", actor="a")
        async with session_scope(session_factory) as session:
            await crud.amend_trace(
                session, org, trace["id"], config, make_rate_limiter(config),
                title="new title", actor="a",
            )

        async with session_scope(session_factory) as session:
            rows = (await session.execute(select(AuditLogEntry))).scalars().all()
        actions = {r.action for r in rows}
        assert {"contribute_trace", "vote_trace", "amend_trace"} <= actions

    async def test_audit_row_rolls_back_with_its_action(self, session_factory, config, org):
        """An audit entry must not survive a transaction that failed -- the
        log should never claim something happened that didn't."""
        rate_limiter = make_rate_limiter(config)
        with pytest.raises(RuntimeError):
            async with session_scope(session_factory) as session:
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="doomed", context_text="c", solution_text="s",
                    tags=[], agent_type="code", actor="a",
                )
                raise RuntimeError("simulated failure after the write")

        async with session_scope(session_factory) as session:
            rows = (await session.execute(select(AuditLogEntry))).scalars().all()
        assert rows == []

    async def test_actor_helper_builds_a_non_secret_string(self):
        assert audit.actor_for_api_key("ct_live_abcd") == "api-key:ct_live_abcd"


class TestApiKeyExpiry:
    async def test_key_without_expiry_still_works(self, session_factory, org):
        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, org)
        async with session_scope(session_factory) as session:
            assert await auth.verify_api_key(session, issued.raw_key) is not None

    async def test_expired_key_is_rejected(self, session_factory, org):
        from datetime import datetime, timedelta, timezone

        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, org, expires_days=30)
        # Move its expiry into the past rather than sleeping.
        async with session_scope(session_factory) as session:
            key = await session.get(ApiKey, issued.key_id)
            key.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        async with session_scope(session_factory) as session:
            assert await auth.verify_api_key(session, issued.raw_key) is None

    async def test_unexpired_key_with_expiry_set_still_works(self, session_factory, org):
        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, org, expires_days=30)
        async with session_scope(session_factory) as session:
            resolved = await auth.verify_api_key(session, issued.raw_key)
        assert resolved is not None and resolved.org_id == org

    async def test_rotation_carries_expiry_policy_forward(self, session_factory, org):
        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, org, expires_days=30)
        async with session_scope(session_factory) as session:
            rotated = await auth.rotate_api_key(session, issued.key_id)
        async with session_scope(session_factory) as session:
            new_key = await session.get(ApiKey, rotated.key_id)
        assert new_key.expires_at is not None, "a 30-day key must not rotate into a never-expiring one"

    async def test_non_positive_expiry_is_rejected(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError):
                await auth.issue_api_key(session, org, expires_days=0)

    async def test_revocation_during_verification_is_honored_not_missed(
        self, session_factory, org, monkeypatch
    ):
        """verify_api_key's initial SELECT filters on `revoked_at IS NULL`,
        then spends most of its time in Argon2 verification (offloaded to a
        worker thread -- deliberately expensive CPU work). An operator's
        revoke_api_key landing in that window used to still authenticate the
        request, because the in-memory candidate loaded before the revoke
        has no way to see a commit that happened after it was loaded. The
        fix re-reads revocation state fresh immediately after verify()
        returns; this simulates a revoke landing exactly inside that
        window by hooking the asyncio.to_thread call verify() is offloaded
        through."""
        import asyncio as asyncio_module

        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, org)

        real_to_thread = asyncio_module.to_thread
        revoked_once = False

        async def revoke_during_verify(func, *args, **kwargs):
            nonlocal revoked_once
            result = await real_to_thread(func, *args, **kwargs)
            if not revoked_once:
                revoked_once = True
                async with session_scope(session_factory) as revoke_session:
                    await auth.revoke_api_key(revoke_session, issued.key_id)
            return result

        monkeypatch.setattr(asyncio_module, "to_thread", revoke_during_verify)

        async with session_scope(session_factory) as session:
            result = await auth.verify_api_key(session, issued.raw_key)
        assert result is None, "a key revoked mid-verification must not authenticate"


class TestVoteTrustAggregate:
    """`vote_trace` recomputes trust from a COUNT/GROUP BY aggregate rather
    than hydrating every vote row for the trace -- this pins the actual
    fraction, not just that the call doesn't crash, since the query shape
    changed.

    `vote_trace`'s trace lookup allows an org to vote on its own trace OR
    any other org's trace currently shared to the commons (hub/crud.py --
    see TestCrossOrgVoting below for the multi-org case this unlocks). A
    given (trace, org) pair still has at most one Vote row, per
    `uq_votes_trace_org`; revoting replaces it rather than accumulating a
    second row. The single-org tests here pin that replace-not-accumulate
    behavior.
    """

    async def test_changing_a_vote_recomputes_trust_not_double_counts_it(
        self, session_factory, config, org
    ):
        """One vote per org per trace: revoting up->down must move the
        tally by one, not add a second row."""
        trace = await _contribute(session_factory, config, org, "t", "c", "s")
        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, org, trace["id"], "up")
        assert result["trust"] == pytest.approx(1.0)

        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, org, trace["id"], "down")
        assert result["trust"] == pytest.approx(0.0)


class TestVoteInputValidation:
    """feedback_tag is constrained to a small enum, and feedback_text has no
    length cap, at the DATABASE layer only (hub/models.py's CheckConstraint /
    unbounded Text). Without matching application-level validation, a bad
    tag reaches the DB's CHECK constraint as an uncaught IntegrityError --
    an opaque HTTP 500 instead of a clean 400 -- and an oversized
    feedback_text is a free storage/audit-log flooding vector (every vote
    writes an AuditLogEntry)."""

    async def test_invalid_feedback_tag_is_a_clean_value_error_not_a_db_crash(
        self, session_factory, config, org
    ):
        trace = await _contribute(session_factory, config, org, "t", "c", "s")
        with pytest.raises(ValueError):
            async with session_scope(session_factory) as session:
                await crud.vote_trace(session, org, trace["id"], "up", feedback_tag="not-a-real-tag")

    async def test_oversized_feedback_text_is_rejected(self, session_factory, config, org):
        from hub.models import MAX_FEEDBACK_TEXT_CHARS

        trace = await _contribute(session_factory, config, org, "t", "c", "s")
        with pytest.raises(ValueError):
            async with session_scope(session_factory) as session:
                await crud.vote_trace(
                    session, org, trace["id"], "up", feedback_text="x" * (MAX_FEEDBACK_TEXT_CHARS + 1)
                )

    async def test_valid_feedback_tag_still_works(self, session_factory, config, org):
        trace = await _contribute(session_factory, config, org, "t", "c", "s")
        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, org, trace["id"], "down", feedback_tag="outdated")
        assert result["votes"][0]["feedback_tag"] == "outdated"


class TestCrossOrgVoting:
    """vote_trace used to scope its trace lookup to `Trace.org_id ==
    org_id` only, which made trust a self-rating: an org's own vote on its
    own trace, never a community signal, even though `trust` is surfaced to
    every other org a shared trace matches for (commons_overlap). An org
    may now also vote on any OTHER org's trace, but only once it is in the
    commons -- a private trace stays exactly as invisible to other orgs as
    every other read path makes it."""

    async def test_another_org_can_vote_on_a_shared_trace(
        self, session_factory, config, org, other_org
    ):
        trace = await _contribute(session_factory, config, org, "t", "c", "s")
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, org, trace["id"])

        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, other_org, trace["id"], "up")
        assert result is not None
        assert result["trust"] == pytest.approx(1.0)

    async def test_another_org_cannot_vote_on_a_private_trace(
        self, session_factory, config, org, other_org
    ):
        trace = await _contribute(session_factory, config, org, "t", "c", "s")
        # Never shared -- must be exactly as unreachable to other_org as
        # get_trace/search_traces already make it.
        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, other_org, trace["id"], "up")
        assert result is None

    async def test_cross_org_vote_response_excludes_private_fields(
        self, session_factory, config, org, other_org
    ):
        """The vote succeeded and the response reflects it (id, trust), but
        a cross-org voter gets the same narrow projection commons_overlap
        returns (H-08) -- voting on someone else's trace is not an
        invitation to see its contributor/extensions/outcome/etc."""
        trace = await _contribute(session_factory, config, org, "t", "c", "s")
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, trace["id"])
            row.contributor = "alice@example.com"
            await crud.share_trace(session, org, trace["id"])

        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, other_org, trace["id"], "down", feedback_tag="outdated")

        assert result["id"] == trace["id"]
        assert result["trust"] == pytest.approx(0.0)
        for private_field in ("contributor", "extensions", "outcome", "votes", "related"):
            assert private_field not in result

    async def test_owner_voting_on_its_own_trace_still_gets_the_full_view(
        self, session_factory, config, org
    ):
        """Unchanged behavior for the owner: full wire shape, including its
        own votes list, same as before this fix."""
        trace = await _contribute(session_factory, config, org, "t", "c", "s")
        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, org, trace["id"], "up")
        assert "votes" in result
        assert result["votes"][0]["vote_type"] == "up"

    async def test_owner_and_another_org_votes_both_count_toward_trust(
        self, session_factory, config, org, other_org
    ):
        trace = await _contribute(session_factory, config, org, "t", "c", "s")
        async with session_scope(session_factory) as session:
            await crud.share_trace(session, org, trace["id"])
            await crud.vote_trace(session, org, trace["id"], "up")
        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, other_org, trace["id"], "down")
        # 1 up (owner) + 1 down (other_org) = 0.5, an actual aggregate
        # across two distinct orgs' votes -- previously unreachable, since
        # only the owner could ever cast one.
        assert result["trust"] == pytest.approx(0.5)


class TestMalformedIdsAreCleanNotFoundNot500s:
    """get_trace/vote_trace/amend_trace/share_trace/unshare_trace all
    compare a caller-supplied trace_id directly against Trace.id, a UUID
    column. asyncpg validates the bind parameter against the column's real
    type -- a non-UUID string raised asyncpg.DataError (wrapped as
    DBAPIError by SQLAlchemy), which is not an IntegrityError and isn't
    caught by any handler in hub/server.py's _error_response, reaching the
    caller as an opaque HTTP 500 instead of the same clean "not found" a
    well-formed-but-nonexistent id already produces."""

    async def test_get_trace_with_a_non_uuid_id_returns_none_not_raises(
        self, session_factory, config, org
    ):
        async with session_scope(session_factory) as session:
            result = await crud.get_trace(session, org, "not-a-uuid-at-all")
        assert result is None

    async def test_vote_trace_with_a_non_uuid_id_returns_none_not_raises(
        self, session_factory, config, org
    ):
        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, org, "not-a-uuid-at-all", "up")
        assert result is None

    async def test_amend_trace_with_a_non_uuid_id_returns_none_not_raises(
        self, session_factory, config, org
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            result = await crud.amend_trace(
                session, org, "not-a-uuid-at-all", config, rate_limiter, title="x", actor="test",
            )
        assert result is None

    async def test_share_trace_with_a_non_uuid_id_returns_none_not_raises(
        self, session_factory, config, org
    ):
        async with session_scope(session_factory) as session:
            result = await crud.share_trace(session, org, "not-a-uuid-at-all")
        assert result is None

    async def test_unshare_trace_with_a_non_uuid_id_returns_none_not_raises(
        self, session_factory, config, org
    ):
        async with session_scope(session_factory) as session:
            result = await crud.unshare_trace(session, org, "not-a-uuid-at-all")
        assert result is None


class TestOversizedIdempotencyKeyIsRejectedCleanly:
    """Trace.idempotency_key is String(128) at the DB layer; a too-long
    value raised asyncpg.StringDataRightTruncation on INSERT -- not an
    IntegrityError, so not caught by contribute_trace's own IntegrityError
    handler, reaching the caller as an HTTP 500 instead of a clean
    rejection of a malformed request."""

    async def test_an_oversized_idempotency_key_is_rejected_before_it_reaches_the_db(
        self, session_factory, config, org
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(TraceRejected):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s",
                    tags=[], agent_type="code", actor="test",
                    idempotency_key="x" * 129,
                )
