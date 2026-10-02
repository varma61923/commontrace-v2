from __future__ import annotations

import pytest
import pytest_asyncio

from hub import crud
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="amend-org")
        session.add(o)
        await session.flush()
        return o.id


async def _contribute_with_extra_fields(session_factory, config, org_id):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        result = await crud.contribute_trace(
            session, org_id, config, rate_limiter,
            title="original title", context_text="c", solution_text="s",
            tags=["a", "b"], agent_type="claude-code", agent_id="agent-42", actor="test",
        )
    async with session_scope(session_factory) as session:
        trace = await session.get(Trace, result["id"])
        trace.watch_condition = "if error_rate > 5%"
        trace.review_after = "2027-01-01"
        trace.contributor = "alice@example.com"
        trace.outcome = {"resolved": True, "notes": "worked"}
        trace.profile = "code-review"
        trace.extensions = {"custom_field": "custom_value"}
    return result["id"]


class TestAmendTraceFieldCarryForward:
    async def test_fields_without_an_override_param_survive_amendment(self, session_factory, config, org):
        trace_id = await _contribute_with_extra_fields(session_factory, config, org)
        rate_limiter = make_rate_limiter(config)

        async with session_scope(session_factory) as session:
            amended = await crud.amend_trace(
                session, org, trace_id, config, rate_limiter, title="new title", actor="test",
            )

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, amended["id"])

        assert row.title == "new title"
        assert row.watch_condition == "if error_rate > 5%"
        assert row.review_after == "2027-01-01"
        assert row.contributor == "alice@example.com"
        assert row.outcome == {"resolved": True, "notes": "worked"}
        assert row.agent_type == "claude-code"
        assert row.agent_id == "agent-42"
        assert row.profile == "code-review"
        assert row.extensions == {"custom_field": "custom_value"}
        assert row.tags == ["a", "b"]
        assert amended["agent_id"] == "agent-42"

    async def test_amending_only_context_leaves_outcome_and_contributor_intact(
        self, session_factory, config, org
    ):
        trace_id = await _contribute_with_extra_fields(session_factory, config, org)
        rate_limiter = make_rate_limiter(config)

        async with session_scope(session_factory) as session:
            amended = await crud.amend_trace(
                session, org, trace_id, config, rate_limiter, context_text="new context", actor="test",
            )

        async with session_scope(session_factory) as session:
            row = await session.get(Trace, amended["id"])

        assert row.context_text == "new context"
        assert row.title == "original title"
        assert row.outcome == {"resolved": True, "notes": "worked"}
        assert row.contributor == "alice@example.com"


class TestAmendTraceQuarantineInheritance:
    async def test_amending_a_quarantined_trace_stays_quarantined(self, session_factory, config, org):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            result = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="original", context_text="c", solution_text="s",
                tags=[], agent_type="code", actor="test",
            )
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, result["id"])
            trace.quarantined = True
            trace.quarantine_reason = "manually flagged for review"

        async with session_scope(session_factory) as session:
            amended = await crud.amend_trace(
                session, org, result["id"], config, rate_limiter,
                title="an innocuous-looking edit", actor="test",
            )

        assert amended["quarantined"] is True
        assert amended["quarantine_reason"] == "manually flagged for review"
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, amended["id"])
        assert row.quarantined is True

    async def test_amending_a_clean_trace_can_still_trip_quarantine(self, session_factory, config, org):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            result = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="original", context_text="c", solution_text="s",
                tags=[], agent_type="code", actor="test",
            )
        spam = " ".join(f"http://spam{i}.example.com" for i in range(20))
        async with session_scope(session_factory) as session:
            amended = await crud.amend_trace(
                session, org, result["id"], config, rate_limiter,
                context_text=spam, actor="test",
            )
        assert amended["quarantined"] is True

    async def test_wire_shape_surfaces_quarantine_status(self, session_factory, config, org):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            result = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s",
                tags=[], agent_type="code", actor="test",
            )
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, result["id"])
            trace.quarantined = True
            trace.quarantine_reason = "why"

        async with session_scope(session_factory) as session:
            fetched = await crud.get_trace(session, org, result["id"])
        assert fetched["quarantined"] is True
        assert fetched["quarantine_reason"] == "why"
