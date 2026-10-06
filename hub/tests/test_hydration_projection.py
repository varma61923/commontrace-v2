"""Hydration keeps batched queries and exact evidence without loading child entities."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event, insert

from hub import crud
from hub.db import session_scope
from hub.models import Organization, Trace, TraceRelation, Vote

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("page_size", [1, 200])
async def test_batched_children_are_exact_and_do_not_enter_identity_map(
    session_factory, page_size,
):
    trace_ids = [str(uuid.uuid4()) for _ in range(page_size + 1)]
    async with session_scope(session_factory) as session:
        owner = Organization(name="hydration owner")
        other = Organization(name="outside page")
        session.add_all([owner, other])
        await session.flush()
        await session.execute(insert(Trace), [
            {
                "id": trace_id,
                "org_id": owner.id if i < page_size else other.id,
                "title": str(i), "context_text": "context", "solution_text": "solution",
                "agent_type": "code",
            }
            for i, trace_id in enumerate(trace_ids)
        ])
        await session.execute(insert(Vote), [
            {
                "trace_id": trace_id, "org_id": owner.id, "vote_type": "down",
                "feedback_tag": "wrong", "feedback_text": "évidence " + str(i),
            }
            for i, trace_id in enumerate(trace_ids)
        ])
        await session.execute(insert(TraceRelation), [
            {
                "trace_id": trace_id, "related_trace_id": str(uuid.uuid4()),
                "relationship_type": relationship,
            }
            for trace_id in trace_ids for relationship in ("supports", "contradicts")
        ])

    async with session_scope(session_factory) as session:
        statements = []
        engine = session.get_bind()

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            votes = await crud._votes_by_trace(session, trace_ids[:page_size])
            related = await crud._related_by_trace(session, trace_ids[:page_size])
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert len(statements) == 2
        assert not session.identity_map
        assert all("created_at" not in stmt and "org_id" not in stmt for stmt in statements)
        assert set(votes) == set(related) == set(trace_ids[:page_size])
        for i, trace_id in enumerate(trace_ids[:page_size]):
            assert votes[trace_id] == [{
                "vote_type": "down", "feedback_tag": "wrong",
                "feedback_text": "évidence " + str(i),
            }]
            assert sorted(r["relationship"] for r in related[trace_id]) == [
                "contradicts", "supports",
            ]
            assert all(set(r) == {"relationship", "trace_id"} for r in related[trace_id])


async def test_empty_hydration_does_not_execute_sql(session_factory):
    async with session_scope(session_factory) as session:
        statements = []
        engine = session.get_bind()

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            assert await crud._hydrate(session, []) == []
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert statements == []
