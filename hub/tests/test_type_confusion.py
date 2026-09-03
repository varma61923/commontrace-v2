"""A non-string (or non-numeric) value in a free-text/numeric field must
get a clean 400, not a crash -- found by a systematic fuzz sweep of every
major crud.py function with deliberately wrong-typed arguments (int, None,
float, list, dict, bool, NaN) in place of every str/float parameter,
after the tags-type-safety fix earlier in this session, to check whether
the same class of bug existed anywhere else undiscovered.

It did, in two distinct shapes:

1. **A len() check running BEFORE its field's reject_unstorable_text
   call**, at four call sites (contribute_trace's idempotency_key and
   agent_id, amend_trace's idempotency_key, submit_kb_entry's rationale
   and idempotency_key, vote_trace's feedback_text). `len(x)` itself
   raises an uncaught TypeError for a non-string x -- not a ValueError --
   so the isinstance check reject_unstorable_text added earlier in this
   session (see test_tags_type_safety.py) never got a chance to run: the
   crash happened one line earlier, before validation, not because
   validation was missing. The fix reorders each site so
   reject_unstorable_text always runs first.

2. **commons_overlap's `threshold = float(threshold)`, with no
   TypeError/ValueError guard at all.** `float(None)`, `float([1])`, and
   `float({"a": 1})` all raise TypeError; the existing
   `math.isfinite(threshold)` check immediately below it (added earlier
   this session for the NaN/Infinity fix) only ever gets a chance to run
   for a value that could already be coerced to a float in the first
   place.

Every case below is reproduced against a live Postgres before the fix,
matching this session's established practice, via the same fuzz harness
that found them (not reconstructed from the fix -- run against the
UNFIXED code first, to confirm each is a real crash and not a
hypothetical one).
"""
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
        """False-positive guard: the fix must not reject ordinary numeric
        input, whether given as a float or a numeric string."""
        async with session_scope(session_factory) as session:
            result = await crud.commons_overlap(session, org, [], threshold=0.5)
        assert result["threshold"] == 0.5
        async with session_scope(session_factory) as session:
            result = await crud.commons_overlap(session, org, [], threshold="0.7")
        assert result["threshold"] == pytest.approx(0.7)
