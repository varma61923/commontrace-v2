"""A NUL byte (0x00) in any free-text field must get a clean 400, not a
crash.

Every string this Hub accepts eventually becomes an asyncpg bind
parameter, and asyncpg refuses to encode a NUL byte into one at all --
Postgres's text type is built on NUL-terminated C strings on-disk, so no
driver can work around it. Before `hub.abuse.reject_embedded_nul`, the
first caller to send one -- a pasted binary log excerpt, a fuzzer, a
client bug -- got `asyncpg.exceptions.CharacterNotInRepertoireError`
raised from deep inside a query, wrapped in a bare `DBAPIError` that
matches none of `hub/server.py:_error_response`'s specific branches. That
falls through to a generic `internal_error` and a server-side stack trace
logged as "unexpected", for input exactly as malformed, and exactly as
foreseeable, as an over-length title -- which every other boundary in this
module already rejects cleanly.

This file pins the fix at every entry point it was found missing:
contribute_trace, amend_trace, and submit_kb_entry's core fields (title,
context_text, solution_text, tags, agent_type, all routed through
`abuse.validate_size`); vote_trace's feedback_text; search_traces's query
and tags; fleet_outcomes/commons_overlap/commons_search's agent_type
filter; holdout_assign/record_occasion_outcome's occasion_id;
submit_kb_entry's rationale and idempotency_key; and the operator paths
retract_kb_entry's reason and review_kb_submission's reviewer/
rejection_reason.

Every one of these is checked against the SAME failure mode with the SAME
assertion shape: call it, expect `ValueError` (never a bare `DBAPIError`,
which is unreachable to any of `_error_response`'s specific branches), and
check the message names the offending field. That repetition is
deliberate -- each is verifying that a specific line of code runs before
the value ever reaches a query, not exercising some general robustness
property, and a table-driven version of this file would hide exactly
which call site regressed if one of them stopped checking.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from hub import commons, crud
from hub.abuse import make_rate_limiter, reject_embedded_nul
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio

NUL = "\x00"


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="nul-org")
        session.add(o)
        await session.flush()
        return o.id


@pytest_asyncio.fixture
async def trace_id(session_factory, org, config):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        t = await crud.contribute_trace(
            session, org, config, rate_limiter,
            title="ok", context_text="c", solution_text="s", tags=[],
            agent_type="code", actor="test",
        )
    return t["id"]


@pytest_asyncio.fixture
async def kb_entry_id(session_factory, org):
    """A live Knowledge Base entry, built the way commons_seed builds one --
    for the retract_kb_entry NUL-reason test, which needs commons_source
    == 'seed' rather than an ordinary contributed trace."""
    async with session_scope(session_factory) as session:
        trace = Trace(
            org_id=org, title="kb entry", context_text="c", solution_text="s",
            tags=[], agent_type="code", shared_with_commons=True,
            shared_at=datetime.now(timezone.utc), shared_rationale="test fixture",
            commons_signature=commons.signature_for("kb entry", "c", []),
            commons_source="seed",
        )
        session.add(trace)
        await session.flush()
        return trace.id


@pytest_asyncio.fixture
async def pending_submission_id(session_factory, org, config):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        s = await crud.submit_kb_entry(
            session, org, config, rate_limiter,
            title="t", context_text="c", solution_text="s",
        )
    return s["id"]


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestRejectEmbeddedNulItself:
    """The shared helper, in isolation."""

    def test_a_clean_string_passes(self):
        reject_embedded_nul("perfectly ordinary text", "field")

    def test_empty_string_passes(self):
        reject_embedded_nul("", "field")

    def test_a_nul_byte_raises_naming_the_field(self):
        with pytest.raises(ValueError, match="field"):
            reject_embedded_nul("bad" + NUL + "value", "field")

    def test_a_nul_byte_anywhere_in_the_string_is_caught(self):
        for value in (NUL, "x" + NUL, NUL + "x", "a" + NUL + "b"):
            with pytest.raises(ValueError):
                reject_embedded_nul(value, "field")

    def test_other_control_characters_are_not_flagged(self):
        """This check is specifically about what Postgres cannot store, not
        a general text sanitizer -- tabs and newlines are ordinary,
        storable content and must not be rejected as if they were NUL."""
        reject_embedded_nul("line one\nline two\ttabbed\r", "field")


class TestContributeTrace:
    async def test_nul_in_title(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="title"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="bad" + NUL + "title", context_text="c", solution_text="s",
                    tags=[], agent_type="code", actor="test",
                )

    async def test_nul_in_context_text(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="context_text"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="bad" + NUL, solution_text="s",
                    tags=[], agent_type="code", actor="test",
                )

    async def test_nul_in_solution_text(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="solution_text"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="bad" + NUL,
                    tags=[], agent_type="code", actor="test",
                )

    async def test_nul_in_a_tag(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="tag"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s",
                    tags=["fine", "bad" + NUL], agent_type="code", actor="test",
                )

    async def test_nul_in_agent_type(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="agent_type"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s",
                    tags=[], agent_type="bad" + NUL, actor="test",
                )

    async def test_nul_in_idempotency_key(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="idempotency_key"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s",
                    tags=[], agent_type="code", actor="test",
                    idempotency_key="bad" + NUL + "key",
                )


class TestAmendTrace:
    """amend_trace routes its wire dict through the same `validate_size`
    contribute_trace does -- see hub/crud.py's own comment on why it needs
    the same four guards. One representative field is enough to pin that
    the shared path is actually exercised; TestContributeTrace above is
    what proves the check itself is correct field-by-field."""

    async def test_nul_in_amended_title(self, session_factory, org, config, trace_id):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="title"):
                await crud.amend_trace(
                    session, org, trace_id, config, rate_limiter,
                    title="bad" + NUL + "title", actor="test",
                )


class TestVoteTrace:
    async def test_nul_in_feedback_text(self, session_factory, org, trace_id):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="feedback_text"):
                await crud.vote_trace(
                    session, org, trace_id, "down",
                    feedback_tag="wrong", feedback_text="bad" + NUL, actor="test",
                )


class TestSearchTraces:
    async def test_nul_in_query(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="query"):
                await crud.search_traces(session, org, query="hello" + NUL + "world")

    async def test_nul_in_a_filter_tag(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="tag"):
                await crud.search_traces(session, org, tags=["fine", "bad" + NUL])

    async def test_a_nul_free_query_still_works(self, session_factory, org, trace_id):
        """The guard must not reject ordinary text -- a false positive here
        would be as much of a regression as missing the true one."""
        async with session_scope(session_factory) as session:
            result = await crud.search_traces(session, org, query="ok")
        assert result["traces"]


class TestFleetOutcomes:
    async def test_nul_in_agent_type(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="agent_type"):
                await crud.fleet_outcomes(session, org, agent_type="bad" + NUL)


class TestKnowledgeBaseQueries:
    async def test_commons_overlap_nul_in_agent_type(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="agent_type"):
                await crud.commons_overlap(session, org, [], agent_type="bad" + NUL)

    async def test_commons_search_nul_in_agent_type(self, session_factory, org):
        signature = commons.signature_for("x", "y", [])
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="agent_type"):
                await crud.commons_search(session, org, signature, agent_type="bad" + NUL)


class TestHoldout:
    async def test_holdout_assign_nul_in_occasion_id(self, session_factory, org, trace_id):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="occasion_id"):
                await crud.holdout_assign(session, org, [trace_id], "bad" + NUL + "occ")

    async def test_record_occasion_outcome_nul_in_occasion_id(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="occasion_id"):
                await crud.record_occasion_outcome(session, org, "bad" + NUL + "occ", True)


class TestKnowledgeBaseSubmissions:
    async def test_submit_kb_entry_nul_in_rationale(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="rationale"):
                await crud.submit_kb_entry(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s",
                    rationale="bad" + NUL,
                )

    async def test_submit_kb_entry_nul_in_idempotency_key(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="idempotency_key"):
                await crud.submit_kb_entry(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s",
                    idempotency_key="bad" + NUL + "key",
                )

    async def test_submit_kb_entry_nul_in_title_via_shared_validate_size(
        self, session_factory, org, config
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="title"):
                await crud.submit_kb_entry(
                    session, org, config, rate_limiter,
                    title="bad" + NUL, context_text="c", solution_text="s",
                )

    async def test_review_kb_submission_nul_in_reviewer(
        self, session_factory, org, pending_submission_id
    ):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="reviewer"):
                await crud.review_kb_submission(
                    session, pending_submission_id, "reject", org,
                    reviewer="bad" + NUL, rejection_reason="fine",
                )

    async def test_review_kb_submission_nul_in_rejection_reason(
        self, session_factory, org, pending_submission_id
    ):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="rejection_reason"):
                await crud.review_kb_submission(
                    session, pending_submission_id, "reject", org,
                    reviewer="test", rejection_reason="bad" + NUL,
                )


class TestRetractKbEntry:
    async def test_nul_in_reason(self, session_factory, kb_entry_id):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="reason"):
                await crud.retract_kb_entry(session, kb_entry_id, reason="bad" + NUL)


class TestSessionIsUsableAfterARejection:
    """A ValueError raised before any write means the session's transaction
    never has anything pending to roll back -- but the surrounding
    `session_scope` still runs its rollback path on any exception, and a
    NEW session_scope opened right after must work normally. This is what
    distinguishes a clean validation error from the DBAPIError it replaces:
    the old failure mode reached Postgres and depended on hub/db.py's
    `except BaseException: rollback` to avoid poisoning the connection
    pool; this one avoids ever touching Postgres in the first place, and
    this test confirms nothing about the fix accidentally leaves the
    session or the pool in a bad state either way."""

    async def test_a_fresh_session_after_a_rejected_call_works_normally(
        self, session_factory, org, config
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="bad" + NUL, context_text="c", solution_text="s",
                    tags=[], agent_type="code", actor="test",
                )
        async with session_scope(session_factory) as session:
            result = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="fine now", context_text="c", solution_text="s",
                tags=[], agent_type="code", actor="test",
            )
        assert result["id"]
        assert result["quarantined"] is False
