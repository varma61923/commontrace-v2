from __future__ import annotations

import pytest
import pytest_asyncio

from hub import commons, crud
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="type-confusion-org")
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


class TestLenRunsAfterTypeCheckNotBefore:
    async def test_contribute_trace_non_string_idempotency_key(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="idempotency_key"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s", tags=[],
                    agent_type="code", actor="test", idempotency_key=123,
                )

    async def test_contribute_trace_non_string_agent_id(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="agent_id"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s", tags=[],
                    agent_type="code", actor="test", agent_id=123,
                )

    async def test_amend_trace_non_string_idempotency_key(self, session_factory, org, config, trace_id):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="idempotency_key"):
                await crud.amend_trace(
                    session, org, trace_id, config, rate_limiter,
                    title="new", actor="test", idempotency_key=123,
                )

    async def test_submit_kb_entry_non_string_rationale(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="rationale"):
                await crud.submit_kb_entry(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s", rationale=123,
                )

    async def test_submit_kb_entry_non_string_idempotency_key(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="idempotency_key"):
                await crud.submit_kb_entry(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s", idempotency_key=123,
                )

    async def test_vote_trace_non_string_feedback_text(self, session_factory, org, trace_id):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="feedback_text"):
                await crud.vote_trace(session, org, trace_id, "up", feedback_text=123, actor="test")

    @pytest.mark.parametrize("bad", [None, 1.5, True, [1], float("nan")], ids=["none", "float", "bool", "list", "nan"])
    async def test_vote_trace_rejects_every_non_string_feedback_text_shape(
        self, session_factory, org, trace_id, bad
    ):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="feedback_text"):
                await crud.vote_trace(session, org, trace_id, "up", feedback_text=bad, actor="test")


class TestCommonsOverlapThreshold:
    @pytest.mark.parametrize("bad", [None, [1], {"a": 1}, "not-a-number"], ids=["none", "list", "dict", "string"])
    async def test_a_non_numeric_threshold_is_rejected_not_crashed(self, session_factory, org, bad):
        async with session_scope(session_factory) as session:
            with pytest.raises(commons.CommonsInputError, match="number"):
                await crud.commons_overlap(session, org, [], threshold=bad)

    async def test_a_normal_threshold_still_works(self, session_factory, org):
        async with session_scope(session_factory) as session:
            result = await crud.commons_overlap(session, org, [], threshold=0.5)
        assert result["threshold"] == 0.5
        async with session_scope(session_factory) as session:
            result = await crud.commons_overlap(session, org, [], threshold="0.7")
        assert result["threshold"] == pytest.approx(0.7)
