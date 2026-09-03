"""A caller-supplied `limit`/`offset`/`credit` of float("inf") must not
crash. `int(x)` raises ValueError for a non-numeric string and TypeError
for None -- both already routine, expected cases -- but OverflowError
specifically for a float infinity (`int(float("inf"))`), which is easy to
miss because it is not the exception either of the other two train you to
expect.

Every one of these fields is otherwise treated as a request to page or
award something reasonable, not as content to validate strictly (unlike
title/context_text/tags, which reject_unstorable_text protects) -- so this
file pins the SAME tolerant behavior each site already had for a merely
out-of-range value (silently clamped, never rejected) now also covers a
malformed one, except commons_search, whose own pre-existing behavior for
a malformed limit was already a clean rejection rather than a clamp, and
which this file confirms still rejects rather than crashes.

hub/commons.py:_coerce_signature carries a standing comment naming this
exact failure mode ("OverflowError on a negative or >2**64-1 value ...
surfacing as an unhandled 500") for a different call site -- this file is
the missing coverage for every OTHER site with the same gap, found by
auditing hub/crud.py for every remaining `int(...)` call on a
caller-supplied value after the NaN/Infinity fix earlier in this session
(hub/outcomes.py:_is_number) caught the equivalent bug in outcome's
numeric fields.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from hub import commons, crud
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization

pytestmark = pytest.mark.asyncio

INF = float("inf")


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="overflow-limit-org")
        session.add(o)
        await session.flush()
        return o.id


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
class TestClampIntItself:
    def test_a_normal_value_within_range_passes_through(self):
        assert crud._clamp_int(10, 1, 100, 50) == 10

    def test_a_value_below_the_floor_is_clamped_up(self):
        assert crud._clamp_int(-5, 1, 100, 50) == 1

    def test_a_value_above_the_ceiling_is_clamped_down(self):
        assert crud._clamp_int(1000, 1, 100, 50) == 100

    @pytest.mark.parametrize("bad", [INF, -INF, float("nan")], ids=["inf", "-inf", "nan"])
    def test_a_non_finite_float_falls_back_to_the_default(self, bad):
        assert crud._clamp_int(bad, 1, 100, 50) == 50

    def test_a_non_numeric_string_falls_back_to_the_default(self):
        assert crud._clamp_int("not a number", 1, 100, 50) == 50

    def test_none_falls_back_to_the_default(self):
        assert crud._clamp_int(None, 1, 100, 50) == 50


class TestSearchTracesLimitOffset:
    async def test_infinite_limit_falls_back_rather_than_crashing(self, session_factory, org):
        async with session_scope(session_factory) as session:
            result = await crud.search_traces(session, org, query="x", limit=INF)
        assert result["limit"] == crud.DEFAULT_SEARCH_LIMIT

    async def test_infinite_offset_falls_back_rather_than_crashing(self, session_factory, org):
        async with session_scope(session_factory) as session:
            result = await crud.search_traces(session, org, query="x", offset=INF)
        assert result["offset"] == 0

    async def test_a_negative_infinity_offset_clamps_to_zero(self, session_factory, org):
        async with session_scope(session_factory) as session:
            result = await crud.search_traces(session, org, query="x", offset=-INF)
        assert result["offset"] == 0


class TestListMyKbSubmissionsLimit:
    async def test_infinite_limit_falls_back_rather_than_crashing(self, session_factory, org):
        async with session_scope(session_factory) as session:
            result = await crud.list_my_kb_submissions(session, org, limit=INF)
        assert result == []


class TestListKbSubmissionsLimit:
    async def test_infinite_limit_falls_back_rather_than_crashing(self, session_factory):
        async with session_scope(session_factory) as session:
            result = await crud.list_kb_submissions(session, limit=INF)
        assert isinstance(result, list)


class TestKbReviewQueueLimit:
    async def test_infinite_limit_falls_back_rather_than_crashing(self, session_factory):
        async with session_scope(session_factory) as session:
            result = await crud.kb_review_queue(session, limit=INF)
        assert isinstance(result, list)


class TestReviewKbSubmissionCredit:
    async def test_infinite_credit_falls_back_to_the_default_award(
        self, session_factory, org, pending_submission_id
    ):
        async with session_scope(session_factory) as session:
            result = await crud.review_kb_submission(
                session, pending_submission_id, "approve", org, reviewer="op", credit=INF,
            )
        assert result["credit_awarded"] == crud.plans.SUBMISSION_ACCEPTANCE_CREDIT


class TestCommonsSearchLimit:
    """Unlike every case above, commons_search's own pre-existing behavior
    for a malformed limit was already a clean rejection (CommonsInputError)
    rather than a silent clamp -- this pins that OverflowError now takes
    the same path as ValueError/TypeError, rather than crashing past it."""

    async def test_infinite_limit_is_rejected_cleanly_not_crashed(self, session_factory, org):
        signature = commons.signature_for("x", "y", [])
        async with session_scope(session_factory) as session:
            with pytest.raises(commons.CommonsInputError, match="integer"):
                await crud.commons_search(session, org, signature, limit=INF)
