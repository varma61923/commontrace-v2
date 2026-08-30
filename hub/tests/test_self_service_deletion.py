"""Self-service deletion: an org acting through its own API key, not an
operator's DB-access-trust-level CLI.

`delete_trace` is immediate and org-scoped -- the same trust level as
every other write tool, since a compromised key could already overwrite a
trace's content via amend_trace. Whole-account deletion is not: a single
`forget_org()` call would let one compromised key wipe an org's entire
history irreversibly with no window for anyone to notice, so it is split
into request_org_deletion / confirm_org_deletion, two differently-named
calls with a mandatory delay between them. See
hub/models.py:Organization's comment on the columns this uses and
hub/crud.py:request_org_deletion's docstring for the full reasoning.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from hub import crud
from hub.abuse import make_rate_limiter
from hub.crud import amend_trace, contribute_trace
from hub.db import session_scope
from hub.models import ApiKey, AuditLogEntry, Organization, Trace, TraceRelation, Vote

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def orgs(session_factory):
    async with session_scope(session_factory) as session:
        made = {}
        for name in ("a", "b"):
            o = Organization(name=name)
            session.add(o)
            await session.flush()
            made[name] = o.id
        return made


class TestDeleteTrace:
    async def test_deletes_the_trace(self, session_factory, config, orgs):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            trace = await contribute_trace(
                session, orgs["a"], config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[], agent_type="code",
            )
        async with session_scope(session_factory) as session:
            deleted = await crud.delete_trace(session, orgs["a"], trace["id"])
        assert deleted is True
        async with session_scope(session_factory) as session:
            assert await session.get(Trace, trace["id"]) is None

    async def test_another_orgs_trace_is_reported_not_found_not_deleted(
        self, session_factory, config, orgs
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            trace = await contribute_trace(
                session, orgs["a"], config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[], agent_type="code",
            )
        async with session_scope(session_factory) as session:
            deleted = await crud.delete_trace(session, orgs["b"], trace["id"])
        assert deleted is False
        async with session_scope(session_factory) as session:
            assert await session.get(Trace, trace["id"]) is not None

    async def test_a_malformed_id_returns_false_not_raises(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            deleted = await crud.delete_trace(session, orgs["a"], "not-a-uuid-at-all")
        assert deleted is False

    async def test_deleting_the_original_also_removes_its_amendment(self, session_factory, config, orgs):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            original = await contribute_trace(
                session, orgs["a"], config, rate_limiter,
                title="MARKER-ORIGINAL", context_text="c", solution_text="s", tags=[], agent_type="code",
            )
        async with session_scope(session_factory) as session:
            amended = await amend_trace(
                session, orgs["a"], original["id"], config, rate_limiter,
                title="MARKER-AMENDED", actor="test",
            )
        async with session_scope(session_factory) as session:
            await crud.delete_trace(session, orgs["a"], original["id"])
        async with session_scope(session_factory) as session:
            assert await session.get(Trace, original["id"]) is None
            assert await session.get(Trace, amended["id"]) is None

    async def test_deleting_the_amendment_also_removes_the_original(self, session_factory, config, orgs):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            original = await contribute_trace(
                session, orgs["a"], config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[], agent_type="code",
            )
        async with session_scope(session_factory) as session:
            amended = await amend_trace(
                session, orgs["a"], original["id"], config, rate_limiter, title="amended", actor="test",
            )
        async with session_scope(session_factory) as session:
            await crud.delete_trace(session, orgs["a"], amended["id"])
        async with session_scope(session_factory) as session:
            assert await session.get(Trace, original["id"]) is None
            assert await session.get(Trace, amended["id"]) is None

    async def test_cleans_up_a_dangling_relation_pointing_at_it(self, session_factory, config, orgs):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            original = await contribute_trace(
                session, orgs["a"], config, rate_limiter,
                title="original", context_text="c", solution_text="s", tags=[], agent_type="code",
            )
            amended = await contribute_trace(
                session, orgs["a"], config, rate_limiter,
                title="amended", context_text="c", solution_text="s", tags=[], agent_type="code",
            )
            session.add(TraceRelation(
                trace_id=original["id"], related_trace_id=amended["id"], relationship_type="SUPERSEDED_BY",
            ))

        async with session_scope(session_factory) as session:
            await crud.delete_trace(session, orgs["a"], amended["id"])

        async with session_scope(session_factory) as session:
            dangling = (
                await session.execute(select(TraceRelation).where(TraceRelation.related_trace_id == amended["id"]))
            ).scalars().all()
        assert dangling == []

    async def test_is_audited(self, session_factory, config, orgs):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            trace = await contribute_trace(
                session, orgs["a"], config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[], agent_type="code",
            )
        async with session_scope(session_factory) as session:
            await crud.delete_trace(session, orgs["a"], trace["id"], actor="api-key:ct_live_abcd")

        async with session_scope(session_factory) as session:
            rows = (
                await session.execute(select(AuditLogEntry).where(AuditLogEntry.action == "delete_trace"))
            ).scalars().all()
        assert len(rows) == 1
        assert rows[0].actor == "api-key:ct_live_abcd"
        assert rows[0].org_id == orgs["a"]
        assert rows[0].target_id == trace["id"]


class TestRequestOrgDeletion:
    async def test_returns_a_token_and_timestamps(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            result = await crud.request_org_deletion(session, orgs["a"])
        assert result["confirmation_token"]
        assert result["confirm_not_before"]
        assert result["expires_at"]

    async def test_unknown_org_raises(self, session_factory):
        with pytest.raises(ValueError):
            async with session_scope(session_factory) as session:
                await crud.request_org_deletion(session, "00000000-0000-0000-0000-000000000000")

    async def test_the_raw_token_is_never_stored(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            result = await crud.request_org_deletion(session, orgs["a"])
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, orgs["a"])
        assert org.deletion_token_hash != result["confirmation_token"]

    async def test_a_second_request_invalidates_the_first_token(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            first = await crud.request_org_deletion(session, orgs["a"])
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, orgs["a"])
            org.deletion_requested_at = datetime.now(timezone.utc) - timedelta(
                seconds=crud.DELETION_GRACE_SECONDS + 10
            )
        async with session_scope(session_factory) as session:
            await crud.request_org_deletion(session, orgs["a"])
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, orgs["a"])
            org.deletion_requested_at = datetime.now(timezone.utc) - timedelta(
                seconds=crud.DELETION_GRACE_SECONDS + 10
            )
        with pytest.raises(crud.DeletionNotReady):
            async with session_scope(session_factory) as session:
                await crud.confirm_org_deletion(session, orgs["a"], first["confirmation_token"])

    async def test_is_audited(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            await crud.request_org_deletion(session, orgs["a"], actor="api-key:ct_live_abcd")
        async with session_scope(session_factory) as session:
            rows = (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.action == "request_org_deletion")
                )
            ).scalars().all()
        assert len(rows) == 1
        assert rows[0].org_id == orgs["a"]


class TestCancelOrgDeletion:
    async def test_cancels_a_pending_request(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            await crud.request_org_deletion(session, orgs["a"])
        async with session_scope(session_factory) as session:
            cancelled = await crud.cancel_org_deletion(session, orgs["a"])
        assert cancelled is True
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, orgs["a"])
        assert org.deletion_token_hash is None
        assert org.deletion_requested_at is None
        assert org.deletion_expires_at is None

    async def test_reports_false_when_nothing_pending(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            cancelled = await crud.cancel_org_deletion(session, orgs["a"])
        assert cancelled is False

    async def test_a_cancelled_token_no_longer_confirms(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            result = await crud.request_org_deletion(session, orgs["a"])
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, orgs["a"])
            org.deletion_requested_at = datetime.now(timezone.utc) - timedelta(
                seconds=crud.DELETION_GRACE_SECONDS + 10
            )
        async with session_scope(session_factory) as session:
            await crud.cancel_org_deletion(session, orgs["a"])
        with pytest.raises(crud.DeletionNotReady):
            async with session_scope(session_factory) as session:
                await crud.confirm_org_deletion(session, orgs["a"], result["confirmation_token"])
        async with session_scope(session_factory) as session:
            assert await session.get(Organization, orgs["a"]) is not None


class TestConfirmOrgDeletion:
    async def _requested_and_ready(self, session_factory, org_id):
        """Request deletion, then move the clock back rather than sleeping
        DELETION_GRACE_SECONDS -- same pattern
        hub/tests/test_search_and_audit.py uses for API key expiry."""
        async with session_scope(session_factory) as session:
            result = await crud.request_org_deletion(session, org_id)
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            org.deletion_requested_at = datetime.now(timezone.utc) - timedelta(
                seconds=crud.DELETION_GRACE_SECONDS + 10
            )
        return result["confirmation_token"]

    async def test_no_pending_request_raises(self, session_factory, orgs):
        with pytest.raises(crud.DeletionNotReady):
            async with session_scope(session_factory) as session:
                await crud.confirm_org_deletion(session, orgs["a"], "whatever")

    async def test_too_soon_raises(self, session_factory, orgs):
        async with session_scope(session_factory) as session:
            result = await crud.request_org_deletion(session, orgs["a"])
        with pytest.raises(crud.DeletionNotReady, match="too soon"):
            async with session_scope(session_factory) as session:
                await crud.confirm_org_deletion(session, orgs["a"], result["confirmation_token"])
        async with session_scope(session_factory) as session:
            assert await session.get(Organization, orgs["a"]) is not None

    async def test_wrong_token_raises_and_deletes_nothing(self, session_factory, orgs):
        await self._requested_and_ready(session_factory, orgs["a"])
        with pytest.raises(crud.DeletionNotReady, match="does not match"):
            async with session_scope(session_factory) as session:
                await crud.confirm_org_deletion(session, orgs["a"], "totally-wrong-token")
        async with session_scope(session_factory) as session:
            assert await session.get(Organization, orgs["a"]) is not None

    async def test_expired_token_raises(self, session_factory, orgs):
        token = await self._requested_and_ready(session_factory, orgs["a"])
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, orgs["a"])
            org.deletion_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        with pytest.raises(crud.DeletionNotReady, match="expired"):
            async with session_scope(session_factory) as session:
                await crud.confirm_org_deletion(session, orgs["a"], token)

    async def test_unknown_org_raises_value_error(self, session_factory):
        with pytest.raises(ValueError):
            async with session_scope(session_factory) as session:
                await crud.confirm_org_deletion(
                    session, "00000000-0000-0000-0000-000000000000", "whatever",
                )

    async def test_the_correct_token_after_the_grace_period_deletes_the_org(
        self, session_factory, orgs
    ):
        token = await self._requested_and_ready(session_factory, orgs["a"])
        async with session_scope(session_factory) as session:
            result = await crud.confirm_org_deletion(session, orgs["a"], token)
        assert result is True
        async with session_scope(session_factory) as session:
            assert await session.get(Organization, orgs["a"]) is None

    async def test_deletion_cascades_to_traces_votes_and_api_keys(
        self, session_factory, config, orgs
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            trace = await contribute_trace(
                session, orgs["a"], config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[], agent_type="code",
            )
            session.add(Vote(trace_id=trace["id"], org_id=orgs["a"], vote_type="up"))
            session.add(ApiKey(org_id=orgs["a"], key_prefix="ct_live_x", key_hash="h"))

        token = await self._requested_and_ready(session_factory, orgs["a"])
        async with session_scope(session_factory) as session:
            await crud.confirm_org_deletion(session, orgs["a"], token)

        async with session_scope(session_factory) as session:
            assert await session.get(Trace, trace["id"]) is None
            n_votes = await session.scalar(select(func.count()).select_from(Vote))
            n_keys = await session.scalar(
                select(func.count()).select_from(ApiKey).where(ApiKey.org_id == orgs["a"])
            )
        assert n_votes == 0
        assert n_keys == 0

    async def test_deletion_cleans_up_a_dangling_relation(self, session_factory, config, orgs):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            original = await contribute_trace(
                session, orgs["a"], config, rate_limiter,
                title="original", context_text="c", solution_text="s", tags=[], agent_type="code",
            )
            amended = await contribute_trace(
                session, orgs["a"], config, rate_limiter,
                title="amended", context_text="c", solution_text="s", tags=[], agent_type="code",
            )
            session.add(TraceRelation(
                trace_id=original["id"], related_trace_id=amended["id"], relationship_type="SUPERSEDED_BY",
            ))

        token = await self._requested_and_ready(session_factory, orgs["a"])
        async with session_scope(session_factory) as session:
            await crud.confirm_org_deletion(session, orgs["a"], token)

        async with session_scope(session_factory) as session:
            dangling = (
                await session.execute(select(TraceRelation).where(TraceRelation.related_trace_id == amended["id"]))
            ).scalars().all()
        assert dangling == []

    async def test_another_orgs_data_is_untouched(self, session_factory, config, orgs):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            other_trace = await contribute_trace(
                session, orgs["b"], config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[], agent_type="code",
            )

        token = await self._requested_and_ready(session_factory, orgs["a"])
        async with session_scope(session_factory) as session:
            await crud.confirm_org_deletion(session, orgs["a"], token)

        async with session_scope(session_factory) as session:
            assert await session.get(Organization, orgs["b"]) is not None
            assert await session.get(Trace, other_trace["id"]) is not None

    async def test_is_audited(self, session_factory, orgs):
        token = await self._requested_and_ready(session_factory, orgs["a"])
        async with session_scope(session_factory) as session:
            await crud.confirm_org_deletion(session, orgs["a"], token, actor="api-key:ct_live_abcd")
        async with session_scope(session_factory) as session:
            rows = (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.action == "confirm_org_deletion")
                )
            ).scalars().all()
        assert len(rows) == 1
        assert rows[0].org_id == orgs["a"]
        assert "irreversible" in rows[0].summary


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestDeletionNotReadyErrorShape:
    """DeletionNotReady is a ValueError subclass but must not collapse into
    the generic invalid_request bucket -- a client needs to tell "your
    request was malformed" apart from "come back later/get a fresh token"."""

    def test_maps_to_its_own_error_code_not_invalid_request(self):
        from hub.server import _error_response

        body = _error_response(crud.DeletionNotReady("too soon: wait 42 more second(s)"))
        assert body["error"] == "deletion_not_ready"
        assert body["error"] != "invalid_request"

    def test_still_a_value_error_for_callers_that_only_check_that(self):
        assert isinstance(crud.DeletionNotReady("x"), ValueError)
