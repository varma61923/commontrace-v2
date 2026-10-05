"""Review pages stay exact without hydrating the full knowledge corpus."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import event, insert

from hub import commons, crud
from hub.db import session_scope
from hub.models import Organization, Trace, Vote

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def owner(session_factory):
    async with session_scope(session_factory) as session:
        org = Organization(name="operator")
        session.add(org)
        await session.flush()
        return org.id


def entry(owner, number, **overrides):
    fields = dict(
        id=str(uuid.UUID(int=number)), org_id=owner, title=f"entry {number}",
        context_text="context", solution_text="solution", tags=[], agent_type="code",
        shared_with_commons=True, commons_source="seed", commons_hits=4,
    )
    fields.update(overrides)
    return fields


async def test_priority_visibility_and_unestablished_security_reports(session_factory, owner):
    now = datetime.now(timezone.utc)
    past, future = now - timedelta(days=1), now + timedelta(days=1)
    entries = [
        entry(owner, 1, commons_hits=0),
        entry(owner, 2, commons_hits=15, commons_review_after=past, trust=0.9, commons_votes=9),
        entry(owner, 3, commons_hits=12, commons_review_after=past, trust=0.49, commons_votes=3),
        entry(owner, 4, commons_hits=6, commons_review_after=past, trust=0.1, commons_votes=3),
        entry(owner, 5, commons_hits=90, commons_review_after=future),
        entry(owner, 6, commons_hits=90, trust=0.5, commons_votes=3),
        entry(owner, 7, commons_hits=3, commons_source="org"),
        entry(owner, 8, commons_hits=3, quarantined=True),
        entry(owner, 9, commons_hits=3, commons_retracted_at=now),
        entry(owner, 10, commons_hits=3, superseded_at=now),
        entry(owner, 11, commons_hits=3, shared_with_commons=False),
    ]
    async with session_scope(session_factory) as session:
        young_voter = Organization(name="young", trace_count=0)
        session.add(young_voter)
        await session.flush()
        await session.execute(insert(Trace), entries)
        # Trust eligibility applies to standing, never to safety reports.
        await session.execute(insert(Vote), [
            dict(trace_id=e["id"], org_id=young_voter.id, vote_type="up",
                 feedback_tag="security_concern")
            for e in entries if int(uuid.UUID(e["id"])) in (4, 7, 8, 9, 10, 11)
        ])
    async with session_scope(session_factory) as session:
        queue, total = await crud.kb_review_queue_and_total(session, limit=2)
        full = await crud.kb_review_queue(session)
        assert not session.identity_map
        assert total == await crud.count_kb_review_queue(session) == 4
    assert [item["id"] for item in full] == [str(uuid.UUID(int=n)) for n in (4, 3, 2, 1)]
    assert queue == full[:2]
    assert [item["bucket"] for item in full] == ["urgent", "disputed", "stale", "never_hit"]
    assert full[0]["standing"] == commons.STANDING_DISPUTED
    assert full[0]["security_flags"] == 1
    assert full[0]["why"] == "1 security concern report(s)"
    assert full[1]["why"] == "3 votes, trust 0.49"
    assert full[2]["standing"] == commons.STANDING_STALE
    assert full[3]["standing"] == commons.STANDING_UNPROVEN


async def test_tied_pages_have_stable_identity_order(session_factory, owner):
    async with session_scope(session_factory) as session:
        await session.execute(insert(Trace), [entry(owner, n, commons_hits=0) for n in (8, 2, 5)])
    async with session_scope(session_factory) as session:
        expected = [str(uuid.UUID(int=n)) for n in (2, 5, 8)]
        for _ in range(3):
            page, total = await crud.kb_review_queue_and_total(session, limit=2)
            assert [item["id"] for item in page] == expected[:2]
            assert total == 3
        assert [item["id"] for item in await crud.kb_review_queue(session)] == expected


async def test_total_is_uncapped_and_combined_page_uses_one_statement(session_factory, owner):
    async with session_scope(session_factory) as session:
        await session.execute(insert(Trace), [entry(owner, n, commons_hits=0) for n in range(1, 508)])
    async with session_scope(session_factory) as session:
        statements = []
        engine = session.get_bind()

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            page, total = await crud.kb_review_queue_and_total(session, limit=100_000)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert total == 507 and len(page) == 500
        assert len(statements) == 1
        assert "context_text" not in statements[0] and "solution_text" not in statements[0]
        assert "search_vector" not in statements[0]
        assert not session.identity_map
        assert await crud.count_kb_review_queue(session) == 507


async def test_empty_page_and_count_agree(session_factory, owner):
    async with session_scope(session_factory) as session:
        await session.execute(insert(Trace), [entry(owner, 1)])
    async with session_scope(session_factory) as session:
        assert await crud.kb_review_queue_and_total(session) == ([], 0)
        assert await crud.count_kb_review_queue(session) == 0
        assert await crud.kb_review_queue(session) == []
