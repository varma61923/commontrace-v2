"""Tests for the production-hardening pass: search pagination + full-text
matching, the N+1 batch-loading fix, audit-log writes, and API-key expiry."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from hub import audit, auth, crud
from hub.abuse import make_rate_limiter
from hub.config import MAX_SEARCH_LIMIT
from hub.db import session_scope
from hub.models import ApiKey, AuditLogEntry, Organization

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="search-org")
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


class TestVoteTrustAggregate:
    """`vote_trace` recomputes trust from a COUNT/GROUP BY aggregate rather
    than hydrating every vote row for the trace -- this pins the actual
    fraction, not just that the call doesn't crash, since the query shape
    changed.

    `vote_trace` scopes its trace lookup to `Trace.org_id == org_id` (same
    tenant-isolation rule as `get_trace`, see hub/crud.py:257-265), so only
    the trace's own owning org can ever vote on it. Combined with the
    `uq_votes_trace_org` unique constraint, that means a given trace can
    have at most one Vote row in practice, ever -- there is no reachable
    multi-org scenario to aggregate across. The only real aggregate
    behavior to pin is a single org's vote being *replaced*, not
    accumulated, on revote.
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
