"""Regression tests for amend_trace's field-carry-forward contract.

amend_trace creates a NEW Trace superseding the original rather than
mutating history in place (hub/crud.py's own docstring). Every field on
Trace needs an explicit classification -- immutable, preserved-by-default,
overridable, or recomputed -- or it silently falls back to its column
default the moment someone amends anything, which is indistinguishable
from data loss. watch_condition, review_after, contributor, and outcome
had no override parameter AND were never passed into the new Trace(...)
constructor call, so they silently reset to "" / {} on every amendment
while agent_type/profile/extensions (which get the same "preserve unless
told otherwise" treatment) correctly survived.
"""
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
            tags=["a", "b"], agent_type="claude-code", actor="test",
        )
    # contribute_trace's MCP-facing signature has no params for these --
    # set them directly to mirror a trace that arrived with richer data
    # (e.g. via a future import/hydration path), matching how the CT-PRIV-001
    # audit populated them to test purge/amend behavior.
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

        assert row.title == "new title"  # the actual override took effect
        # everything else must carry forward unchanged -- not reset to
        # column defaults ("" / {}) just because it wasn't re-specified
        assert row.watch_condition == "if error_rate > 5%"
        assert row.review_after == "2027-01-01"
        assert row.contributor == "alice@example.com"
        assert row.outcome == {"resolved": True, "notes": "worked"}
        assert row.agent_type == "claude-code"
        assert row.profile == "code-review"
        assert row.extensions == {"custom_field": "custom_value"}
        assert row.tags == ["a", "b"]

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
    """amend_trace used to run only its own heuristic on the amended
    content, ignoring whether the trace it supersedes was already
    quarantined. That is an unsupervised way around a state that is
    supposed to require an operator's release_quarantine to lift: quarantine
    a trace, amend it with a small edit that happens not to trip
    suspicion_reason on the new text, and the successor comes back clean."""

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
        """The inheritance fix must not stop amend_trace's own heuristic
        from still catching newly-suspicious content on a previously clean
        trace."""
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
        """get_trace/vote_trace do not filter quarantine the way
        search_traces/list_tags do, so a caller reaching its own quarantined
        trace by id needs some visible signal that it is quarantined rather
        than getting the full body back looking like any other trace."""
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
