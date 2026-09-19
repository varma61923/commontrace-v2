"""Tests for the production-hardening pass: search pagination + full-text
matching, the N+1 batch-loading fix, audit-log writes, and API-key expiry."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from hub import audit, auth, commons, crud
from hub.abuse import TraceRejected, make_rate_limiter
from hub.config import MAX_SEARCH_LIMIT
from hub.db import session_scope
from hub.models import ApiKey, AuditLogEntry, Organization, Trace, Vote

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


async def _contribute(
    session_factory, config, org_id, title, context, solution, tags=None, actor="test", outcome=None,
):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        return await crud.contribute_trace(
            session, org_id, config, rate_limiter,
            title=title, context_text=context, solution_text=solution,
            tags=tags or [], agent_type="code", actor=actor, outcome=outcome,
        )


async def _seed_kb(session_factory, operator_org_id, title, context="c", solution="s", contributor=None):
    """A Knowledge Base entry, seeded directly the way
    hub/manage.py:commons_seed does it -- the only way one exists in
    production. vote_trace's cross-org path is only reachable for these
    (commons_source == "seed"), never for another org's private trace."""
    async with session_scope(session_factory) as session:
        trace = Trace(
            org_id=operator_org_id,
            title=title, context_text=context, solution_text=solution,
            tags=[], agent_type="code",
            shared_with_commons=True,
            shared_at=datetime.now(timezone.utc),
            shared_rationale="test fixture",
            commons_signature=commons.signature_for(title, context, []),
            commons_source="seed",
            contributor=contributor,
        )
        session.add(trace)
        await session.flush()
        return trace.id


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

    async def test_offset_is_capped_not_left_unbounded(self, session_factory, config, org):
        """OFFSET pagination costs Postgres work proportional to the offset
        itself -- it still has to walk and discard every skipped row. An
        unbounded caller-supplied offset turned one request into a scan of
        the org's entire trace table just to throw the results away."""
        from hub.config import MAX_SEARCH_OFFSET

        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, offset=MAX_SEARCH_OFFSET + 50_000)
        assert page["offset"] == MAX_SEARCH_OFFSET


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


class TestRelevanceTieOrdering:
    """Exact ts_rank ties are the signature of near-duplicate text, and a
    fleet produces those constantly: it resolves an occasion using a
    lesson, then contributes a trace saying the same thing in the same
    words. Inside a tie the ORIGINAL is the better result to hand an
    agent, and keeping it on the page is also what lets the near-duplicate
    clustering hold a stable randomization unit."""

    async def test_the_original_wins_a_relevance_tie_against_its_own_retellings(
        self, session_factory, config, org
    ):
        original = await _contribute(
            session_factory, config, org, "pool exhausted",
            "connection pool exhausted running the suite", "dispose the engine",
        )
        for _ in range(4):
            await _contribute(
                session_factory, config, org, "pool exhausted",
                "connection pool exhausted running the suite", "dispose the engine",
            )
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(
                session, org, query="connection pool exhausted running the suite", limit=3
            )
        assert page["traces"][0]["id"] == original["id"]

    async def test_the_original_stays_on_page_one_as_retellings_accumulate(
        self, session_factory, config, org
    ):
        """The property the clustering depends on: an original that falls
        off the page once enough re-tellings exist leaves a page with no
        fixed member to anchor a randomization unit to."""
        original = await _contribute(
            session_factory, config, org, "pool exhausted",
            "connection pool exhausted running the suite", "dispose the engine",
        )
        for _ in range(12):
            await _contribute(
                session_factory, config, org, "pool exhausted",
                "connection pool exhausted running the suite", "dispose the engine",
            )
            async with session_scope(session_factory) as session:
                page = await crud.search_traces(
                    session, org, query="connection pool exhausted running the suite", limit=5
                )
            assert original["id"] in {t["id"] for t in page["traces"]}

    async def test_recency_still_orders_the_no_query_browse_path(
        self, session_factory, config, org
    ):
        """Oldest-first applies inside a relevance tie, not to browsing --
        `search_traces` with no query is a recency feed and stays one."""
        await _contribute(session_factory, config, org, "first", "c", "s")
        newest = await _contribute(session_factory, config, org, "second", "c", "s")
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, query="")
        assert page["traces"][0]["id"] == newest["id"]


class TestFailedOutcomeRanking:
    """A trace whose own `outcome.resolved` is False -- an agent's
    self-logged, unresolved attempt, not a curated solution -- must never
    outrank a same-relevance trace with no such marker. Text relevance
    alone cannot separate them: both describe the same failure in the same
    words, so without this a hand-written lesson and a fleet's own escalated
    retry of the same query rank on equal footing."""

    async def test_a_failed_occasion_sorts_after_an_otherwise_equal_result(
        self, session_factory, config, org
    ):
        await _contribute(
            session_factory, config, org, "escalated attempt",
            "connection pool exhausted running tests", "gave up, escalated",
            outcome={"resolved": False, "escalated": True},
        )
        await _contribute(
            session_factory, config, org, "the actual fix",
            "connection pool exhausted running tests", "dispose the engine in teardown",
        )
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, query="connection pool exhausted tests")
        titles = [t["title"] for t in page["traces"]]
        assert titles == ["the actual fix", "escalated attempt"]

    async def test_a_resolved_occasion_is_not_demoted(self, session_factory, config, org):
        """The floor is specifically for a recorded FAILURE, not for having
        an outcome at all -- a successfully resolved occasion competes on
        relevance exactly as before."""
        await _contribute(
            session_factory, config, org, "resolved once",
            "connection pool exhausted running tests", "dispose the engine",
            outcome={"resolved": True},
        )
        await _contribute(
            session_factory, config, org, "no outcome recorded",
            "connection pool exhausted running tests", "dispose the engine",
        )
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, query="connection pool exhausted tests")
        # Both equally eligible for the top spot -- neither is demoted.
        assert {t["title"] for t in page["traces"][:2]} == {"resolved once", "no outcome recorded"}

    async def test_the_floor_also_applies_with_no_query(self, session_factory, config, org):
        await _contribute(
            session_factory, config, org, "failed", "c", "unresolved",
            outcome={"resolved": False},
        )
        await _contribute(session_factory, config, org, "clean", "c", "s")
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, query="")
        assert page["traces"][-1]["title"] == "failed"

    async def test_a_failed_result_is_still_returned_not_dropped(
        self, session_factory, config, org
    ):
        """A ranking floor, not a filter: still findable, just never ahead
        of a better-standing result for the same query."""
        await _contribute(
            session_factory, config, org, "only match",
            "extremely specific unmatched vocabulary here", "unresolved",
            outcome={"resolved": False},
        )
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, query="extremely specific unmatched vocabulary")
        assert len(page["traces"]) == 1


class TestBriefMode:
    """context_text/solution_text are each allowed up to 20,000 characters
    (HubConfig.max_text_chars), so a full page at MAX_SEARCH_LIMIT can
    legitimately run to millions of characters -- enough to blow a calling
    agent's own context budget, not just its bill. `brief=True` previews
    both fields instead."""

    async def test_default_behavior_is_unchanged(self, session_factory, config, org):
        """Off by default -- an existing caller reading context_text/
        solution_text straight off a search result must keep working
        exactly as before."""
        await _contribute(session_factory, config, org, "t", "short context", "short solution")
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org)
        trace = page["traces"][0]
        assert trace["context_text"] == "short context"
        assert trace["solution_text"] == "short solution"
        assert "brief" not in trace

    async def test_a_short_field_is_returned_whole_but_marked_brief(self, session_factory, config, org):
        """Short enough to need no truncation is not the same claim as
        'this is the full record' -- brief=True always marks its output,
        whether or not anything was actually cut."""
        await _contribute(session_factory, config, org, "t", "short context", "short solution")
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, brief=True)
        trace = page["traces"][0]
        assert trace["context_text"] == "short context"
        assert trace["solution_text"] == "short solution"
        assert trace["brief"] is True

    async def test_a_long_field_is_truncated_with_an_ellipsis(self, session_factory, config, org):
        long_context = "word " * 500  # far past BRIEF_PREVIEW_CHARS
        await _contribute(session_factory, config, org, "t", long_context, "short solution")
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, brief=True)
        trace = page["traces"][0]
        assert len(trace["context_text"]) < len(long_context)
        assert trace["context_text"].endswith("…")

    async def test_truncation_cuts_at_a_word_boundary(self, session_factory, config, org):
        long_context = "alpha " * 500
        await _contribute(session_factory, config, org, "t", long_context, "s")
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, brief=True)
        preview = page["traces"][0]["context_text"]
        # Never ends mid-word: strip the ellipsis and the remainder must be
        # whole "alpha" tokens, not a fragment like "alph".
        body = preview.rstrip("…")
        assert body == "" or body.split()[-1] == "alpha"

    async def test_ids_titles_and_tags_are_unaffected_by_brief(self, session_factory, config, org):
        await _contribute(
            session_factory, config, org, "unaffected title", "c" * 1000, "s" * 1000, tags=["x", "y"]
        )
        async with session_scope(session_factory) as session:
            full = await crud.search_traces(session, org, brief=False)
            brief = await crud.search_traces(session, org, brief=True)
        assert full["traces"][0]["id"] == brief["traces"][0]["id"]
        assert full["traces"][0]["title"] == brief["traces"][0]["title"] == "unaffected title"
        assert full["traces"][0]["tags"] == brief["traces"][0]["tags"] == ["x", "y"]

    async def test_get_trace_is_never_brief(self, session_factory, config, org):
        """brief is a search_traces-only concept -- fetching one trace by id
        to actually use it must always return the whole thing."""
        long_context = "word " * 500
        contributed = await _contribute(session_factory, config, org, "t", long_context, "s")
        async with session_scope(session_factory) as session:
            trace = await crud.get_trace(session, org, contributed["id"])
        assert trace["context_text"] == long_context
        assert "brief" not in trace

    async def test_default_valued_operational_fields_are_omitted_in_brief_mode(
        self, session_factory, config, org
    ):
        """`brief=True` exists so 'browse many, then get_trace the one you
        pick' costs less than one non-brief call -- which measurably failed
        while every one of these ~14 fields was always present, even at
        their empty/false/zero default, on every brief result. None of them
        were set on this trace, so none of them should ship."""
        await _contribute(session_factory, config, org, "t", "some context", "some solution")
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, brief=True)
        trace = page["traces"][0]
        # Not `retrievals`: search_traces increments it (as a side effect
        # of being returned by THIS call) before hydrating the wire dict,
        # so it is never actually 0 on a result -- pre-existing behavior,
        # unrelated to this trimming.
        for field in (
            "agent_id", "profile", "extensions", "watch_condition", "review_after",
            "supersedes_trace_id", "contributor", "depth", "votes",
            "related", "outcome", "shared_with_commons", "quarantine_reason",
        ):
            assert field not in trace, f"{field!r} should be omitted at its default in brief mode"
        assert trace["retrievals"] == 1
        # Always present regardless -- never conditionally dropped.
        for field in ("id", "title", "context_text", "solution_text", "tags",
                      "agent_type", "created_at", "trust", "quarantined", "brief"):
            assert field in trace

    async def test_a_populated_operational_field_still_ships_in_brief_mode(
        self, session_factory, config, org
    ):
        """Only the DEFAULT value is omitted -- a field actually holding
        something must still reach the caller in brief mode, same as full."""
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="some context", solution_text="some solution",
                tags=[], agent_type="code",
                outcome={"resolved": True, "tokens_used": 42},
            )
            page = await crud.search_traces(session, org, brief=True)
        trace = page["traces"][0]
        assert trace["outcome"] == {"resolved": True, "tokens_used": 42}

    async def test_full_mode_still_ships_every_field_at_its_default(
        self, session_factory, config, org
    ):
        """The trimming above is brief-only -- an existing full-mode caller
        reading any of these fields off a normal result must see exactly
        what it always has, default value included."""
        await _contribute(session_factory, config, org, "t", "some context", "some solution")
        async with session_scope(session_factory) as session:
            page = await crud.search_traces(session, org, brief=False)
        trace = page["traces"][0]
        assert trace["agent_id"] == ""
        assert trace["votes"] == []
        assert trace["outcome"] == {}
        assert trace["shared_with_commons"] is False


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
        through.

        This window exists only in the legacy (Argon2 prefix-scan) path --
        the fast `key_hmac` lookup added later reads revocation in the same
        single indexed SELECT that finds the row, with no `to_thread` call
        and no gap for a concurrent revoke to land in. A freshly issued key
        now has `key_hmac` set at issuance and would resolve via that fast
        path, never calling `asyncio.to_thread` at all -- clear it here to
        force this key through the legacy path this test exercises, exactly
        as a key issued before that column existed would be."""
        import asyncio as asyncio_module

        from sqlalchemy import update

        from hub.models import ApiKey

        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, org)
        async with session_scope(session_factory) as session:
            await session.execute(
                update(ApiKey).where(ApiKey.id == issued.key_id).values(key_hmac=None)
            )

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


class TestLastUsedAtIsThrottled:
    """last_used_at exists for idle-key auditing, which needs roughly-
    current information, not per-request precision. Writing it
    unconditionally means a hot key under real QPS issues an UPDATE
    against its own single row on every authenticated request -- every one
    of those write transactions briefly locks the same row, serializing
    concurrent requests against each other for no operational benefit."""

    async def test_a_fresh_last_used_at_is_not_rewritten_on_the_next_call(
        self, session_factory, org
    ):
        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, org)
        async with session_scope(session_factory) as session:
            await auth.verify_api_key(session, issued.raw_key)
        async with session_scope(session_factory) as session:
            first_seen = (await session.get(ApiKey, issued.key_id)).last_used_at

        async with session_scope(session_factory) as session:
            await auth.verify_api_key(session, issued.raw_key)
        async with session_scope(session_factory) as session:
            second_seen = (await session.get(ApiKey, issued.key_id)).last_used_at

        assert first_seen == second_seen, "a call within the throttle interval must not rewrite the column"

    async def test_a_stale_last_used_at_is_refreshed(self, session_factory, org):
        from datetime import datetime, timedelta, timezone

        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, org)
        stale = datetime.now(timezone.utc) - timedelta(hours=1)
        async with session_scope(session_factory) as session:
            key = await session.get(ApiKey, issued.key_id)
            key.last_used_at = stale

        async with session_scope(session_factory) as session:
            await auth.verify_api_key(session, issued.raw_key)
        async with session_scope(session_factory) as session:
            refreshed = (await session.get(ApiKey, issued.key_id)).last_used_at

        assert refreshed > stale

    async def test_a_never_used_key_gets_last_used_at_set_on_first_call(
        self, session_factory, org
    ):
        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, org)
        async with session_scope(session_factory) as session:
            key = await session.get(ApiKey, issued.key_id)
            assert key.last_used_at is None

        async with session_scope(session_factory) as session:
            await auth.verify_api_key(session, issued.raw_key)
        async with session_scope(session_factory) as session:
            assert (await session.get(ApiKey, issued.key_id)).last_used_at is not None


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

    async def test_invalid_vote_type_is_a_clean_value_error(self, session_factory, config, org):
        trace = await _contribute(session_factory, config, org, "t", "c", "s")
        with pytest.raises(ValueError, match="vote_type"):
            async with session_scope(session_factory) as session:
                await crud.vote_trace(session, org, trace["id"], "sideways")


class TestCrossOrgVoting:
    """vote_trace used to scope its trace lookup to `Trace.org_id ==
    org_id` only, which made trust a self-rating. It now also reaches a
    Knowledge Base entry (`commons_source == "seed"`) regardless of which
    org is voting, since `trust` is surfaced to every org a Knowledge Base
    entry matches for (commons_overlap/commons_search). A private trace --
    one that never entered the Knowledge Base -- stays exactly as invisible
    to other orgs as every other read path makes it; there is no org-to-org
    path here at all, only org-to-Knowledge-Base."""

    async def test_another_org_can_vote_on_a_kb_entry(
        self, session_factory, org, other_org, establish_orgs
    ):
        trace_id = await _seed_kb(session_factory, org, "t")
        await establish_orgs(other_org)
        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, other_org, trace_id, "up")
        assert result is not None
        assert result["trust"] == pytest.approx(1.0)

    async def test_another_org_cannot_vote_on_a_private_trace(
        self, session_factory, config, org, other_org
    ):
        trace = await _contribute(session_factory, config, org, "t", "c", "s")
        # Never seeded into the Knowledge Base -- must be exactly as
        # unreachable to other_org as get_trace/search_traces already make it.
        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, other_org, trace["id"], "up")
        assert result is None

    async def test_cross_org_vote_response_excludes_private_fields(
        self, session_factory, org, other_org, establish_orgs
    ):
        """The vote succeeded and the response reflects it (id, trust), but
        a cross-org voter gets the same narrow projection commons_overlap
        returns (H-08) -- voting on a Knowledge Base entry is not an
        invitation to see its contributor/extensions/outcome/etc."""
        trace_id = await _seed_kb(session_factory, org, "t", contributor="alice@example.com")
        await establish_orgs(other_org)

        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, other_org, trace_id, "down", feedback_tag="outdated")

        assert result["id"] == trace_id
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
        self, session_factory, org, other_org, establish_orgs
    ):
        trace_id = await _seed_kb(session_factory, org, "t")
        # BOTH established. Without this the assertion below still passed,
        # for the wrong reason: no vote counted at all, and `trust` fell
        # back to its 0.5 no-votes default -- numerically identical to the
        # 1-up-1-down aggregate this test exists to check. A test that can
        # pass while counting nothing is not testing the aggregate.
        await establish_orgs(org, other_org)
        async with session_scope(session_factory) as session:
            await crud.vote_trace(session, org, trace_id, "up")
        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, other_org, trace_id, "down")
        # 1 up (the seeding org) + 1 down (other_org) = 0.5, an actual
        # aggregate across two distinct orgs' votes.
        assert result["trust"] == pytest.approx(0.5)
        assert result["vote_count"] == 2

    async def test_the_owners_own_vote_cannot_flush_in_held_back_votes(
        self, session_factory, org, other_org, establish_orgs
    ):
        """The bar is keyed on the TRACE being a Knowledge Base entry, not
        on who is casting the vote -- and this is why.

        Keyed on the voter instead ("an org rating its own trace needs no
        bar"), the entry owner's single vote would take the unfiltered
        path and count EVERY stored vote, including the ones the filtered
        path had been holding out. A farm that could not move the number
        directly would move it by waiting for the operator to vote once.
        """
        trace_id = await _seed_kb(session_factory, org, "t")
        await establish_orgs(org)
        for i in range(6):
            async with session_scope(session_factory) as session:
                sock = Organization(name=f"flush-sock-{i}")
                session.add(sock)
                await session.flush()
                sock_id = sock.id
            async with session_scope(session_factory) as session:
                await crud.vote_trace(session, sock_id, trace_id, "down")

        async with session_scope(session_factory) as session:
            result = await crud.vote_trace(session, org, trace_id, "up")
        assert result is not None

        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            stored = (
                await session.execute(select(Vote).where(Vote.trace_id == trace_id))
            ).scalars().all()

        # Only the owner's own (established) vote moved the published pair.
        assert trace.commons_votes == 1
        assert trace.trust == pytest.approx(1.0)
        # The six held-back down-votes are still on record -- withheld from
        # the tally, never discarded.
        assert len(stored) == 7


class TestMalformedIdsAreCleanNotFoundNot500s:
    """get_trace/vote_trace/amend_trace all compare a caller-supplied
    trace_id directly against Trace.id, a UUID column. asyncpg validates
    the bind parameter against the column's real type -- a non-UUID string
    raised asyncpg.DataError (wrapped as DBAPIError by SQLAlchemy), which is
    not an IntegrityError and isn't caught by any handler in
    hub/server.py's _error_response, reaching the caller as an opaque HTTP
    500 instead of the same clean "not found" a well-formed-but-nonexistent
    id already produces."""

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

    async def test_amend_trace_oversized_idempotency_key_is_rejected(self, session_factory, config, org):
        trace = await _contribute(session_factory, config, org, "t", "c", "s")
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(TraceRejected):
                await crud.amend_trace(
                    session, org, trace["id"], config, rate_limiter,
                    title="new", actor="test", idempotency_key="x" * 129,
                )

    async def test_submit_kb_entry_oversized_idempotency_key_is_rejected(self, session_factory, config, org):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(TraceRejected):
                await crud.submit_kb_entry(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s",
                    idempotency_key="x" * 129,
                )
