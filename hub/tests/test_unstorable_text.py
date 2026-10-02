from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from hub import commons, crud
from hub.abuse import make_rate_limiter, reject_unstorable_text
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio

NUL = "\x00"
SURROGATE = "\ud800"
BAD_FRAGMENTS = pytest.mark.parametrize("bad", [NUL, SURROGATE], ids=["nul", "surrogate"])


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="unstorable-text-org")
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
class TestRejectUnstorableTextItself:
    def test_a_clean_string_passes(self):
        reject_unstorable_text("perfectly ordinary text", "field")

    def test_empty_string_passes(self):
        reject_unstorable_text("", "field")

    def test_a_properly_paired_supplementary_character_passes(self):
        reject_unstorable_text("great work \U0001F600", "field")

    @BAD_FRAGMENTS
    def test_a_bad_fragment_raises_naming_the_field(self, bad):
        with pytest.raises(ValueError, match="field"):
            reject_unstorable_text("bad" + bad + "value", "field")

    @BAD_FRAGMENTS
    def test_a_bad_fragment_anywhere_in_the_string_is_caught(self, bad):
        for value in (bad, "x" + bad, bad + "x", "a" + bad + "b"):
            with pytest.raises(ValueError):
                reject_unstorable_text(value, "field")

    def test_other_control_characters_are_not_flagged(self):
        reject_unstorable_text("line one\nline two\ttabbed\r", "field")


class TestContributeTrace:
    @BAD_FRAGMENTS
    async def test_bad_fragment_in_title(self, session_factory, org, config, bad):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="title"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="bad" + bad + "title", context_text="c", solution_text="s",
                    tags=[], agent_type="code", actor="test",
                )

    @BAD_FRAGMENTS
    async def test_bad_fragment_in_context_text(self, session_factory, org, config, bad):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="context_text"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="bad" + bad, solution_text="s",
                    tags=[], agent_type="code", actor="test",
                )

    @BAD_FRAGMENTS
    async def test_bad_fragment_in_solution_text(self, session_factory, org, config, bad):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="solution_text"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="bad" + bad,
                    tags=[], agent_type="code", actor="test",
                )

    @BAD_FRAGMENTS
    async def test_bad_fragment_in_a_tag(self, session_factory, org, config, bad):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="tag"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s",
                    tags=["fine", "bad" + bad], agent_type="code", actor="test",
                )

    @BAD_FRAGMENTS
    async def test_bad_fragment_in_agent_type(self, session_factory, org, config, bad):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="agent_type"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s",
                    tags=[], agent_type="bad" + bad, actor="test",
                )

    @BAD_FRAGMENTS
    async def test_bad_fragment_in_idempotency_key(self, session_factory, org, config, bad):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="idempotency_key"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s",
                    tags=[], agent_type="code", actor="test",
                    idempotency_key="bad" + bad + "key",
                )

    @BAD_FRAGMENTS
    async def test_bad_fragment_in_agent_id(self, session_factory, org, config, bad):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="agent_id"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s",
                    tags=[], agent_type="code", actor="test",
                    agent_id="bad" + bad + "id",
                )


class TestAmendTrace:
    @BAD_FRAGMENTS
    async def test_bad_fragment_in_amended_title(self, session_factory, org, config, trace_id, bad):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="title"):
                await crud.amend_trace(
                    session, org, trace_id, config, rate_limiter,
                    title="bad" + bad + "title", actor="test",
                )


class TestVoteTrace:
    @BAD_FRAGMENTS
    async def test_bad_fragment_in_feedback_text(self, session_factory, org, trace_id, bad):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="feedback_text"):
                await crud.vote_trace(
                    session, org, trace_id, "down",
                    feedback_tag="wrong", feedback_text="bad" + bad, actor="test",
                )


class TestSearchTraces:
    @BAD_FRAGMENTS
    async def test_bad_fragment_in_query(self, session_factory, org, bad):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="query"):
                await crud.search_traces(session, org, query="hello" + bad + "world")

    @BAD_FRAGMENTS
    async def test_bad_fragment_in_a_filter_tag(self, session_factory, org, bad):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="tag"):
                await crud.search_traces(session, org, tags=["fine", "bad" + bad])

    async def test_a_clean_query_still_works(self, session_factory, org, trace_id):
        async with session_scope(session_factory) as session:
            result = await crud.search_traces(session, org, query="ok")
        assert result["traces"]


class TestFleetOutcomes:
    @BAD_FRAGMENTS
    async def test_bad_fragment_in_agent_type(self, session_factory, org, bad):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="agent_type"):
                await crud.fleet_outcomes(session, org, agent_type="bad" + bad)


class TestKnowledgeBaseQueries:
    @BAD_FRAGMENTS
    async def test_commons_overlap_bad_fragment_in_agent_type(self, session_factory, org, bad):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="agent_type"):
                await crud.commons_overlap(session, org, [], agent_type="bad" + bad)

    @BAD_FRAGMENTS
    async def test_commons_search_bad_fragment_in_agent_type(self, session_factory, org, bad):
        signature = commons.signature_for("x", "y", [])
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="agent_type"):
                await crud.commons_search(session, org, signature, agent_type="bad" + bad)


class TestHoldout:
    @BAD_FRAGMENTS
    async def test_holdout_assign_bad_fragment_in_occasion_id(self, session_factory, org, trace_id, bad):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="occasion_id"):
                await crud.holdout_assign(session, org, [trace_id], "bad" + bad + "occ")

    @BAD_FRAGMENTS
    async def test_record_occasion_outcome_bad_fragment_in_occasion_id(self, session_factory, org, bad):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="occasion_id"):
                await crud.record_occasion_outcome(session, org, "bad" + bad + "occ", True)


class TestKnowledgeBaseSubmissions:
    @BAD_FRAGMENTS
    async def test_submit_kb_entry_bad_fragment_in_rationale(self, session_factory, org, config, bad):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="rationale"):
                await crud.submit_kb_entry(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s",
                    rationale="bad" + bad,
                )

    @BAD_FRAGMENTS
    async def test_submit_kb_entry_bad_fragment_in_idempotency_key(self, session_factory, org, config, bad):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="idempotency_key"):
                await crud.submit_kb_entry(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s",
                    idempotency_key="bad" + bad + "key",
                )

    @BAD_FRAGMENTS
    async def test_submit_kb_entry_bad_fragment_in_title_via_shared_validate_size(
        self, session_factory, org, config, bad
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="title"):
                await crud.submit_kb_entry(
                    session, org, config, rate_limiter,
                    title="bad" + bad, context_text="c", solution_text="s",
                )

    @BAD_FRAGMENTS
    async def test_review_kb_submission_bad_fragment_in_reviewer(
        self, session_factory, org, pending_submission_id, bad
    ):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="reviewer"):
                await crud.review_kb_submission(
                    session, pending_submission_id, "reject", org,
                    reviewer="bad" + bad, rejection_reason="fine",
                )

    @BAD_FRAGMENTS
    async def test_review_kb_submission_bad_fragment_in_rejection_reason(
        self, session_factory, org, pending_submission_id, bad
    ):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="rejection_reason"):
                await crud.review_kb_submission(
                    session, pending_submission_id, "reject", org,
                    reviewer="test", rejection_reason="bad" + bad,
                )


class TestRetractKbEntry:
    @BAD_FRAGMENTS
    async def test_bad_fragment_in_reason(self, session_factory, kb_entry_id, bad):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="reason"):
                await crud.retract_kb_entry(session, kb_entry_id, reason="bad" + bad)


class TestSessionIsUsableAfterARejection:
    @BAD_FRAGMENTS
    async def test_a_fresh_session_after_a_rejected_call_works_normally(
        self, session_factory, org, config, bad
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="bad" + bad, context_text="c", solution_text="s",
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
