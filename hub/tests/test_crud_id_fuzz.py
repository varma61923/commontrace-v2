from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from hub import crud
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio

HOSTILE = [
    "../../etc/passwd", "' OR '1'='1", "1; DROP TABLE traces;--", "%00", "‮",
    "-1", "{{7*7}}", "A" * 1500, "", "00000000-0000-0000-0000-000000000000",
]


@pytest_asyncio.fixture
async def two_orgs(session_factory, config):
    limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        a, b = Organization(name="fuzz-a"), Organization(name="fuzz-b")
        session.add_all([a, b])
        await session.flush()
        a_id, b_id = a.id, b.id
    async with session_scope(session_factory) as session:
        made = await crud.contribute_trace(
            session, b_id, config, limiter, title="b private title",
            context_text="b private context about a staging outage",
            solution_text="b private solution: rotate the credentials",
            tags=["b"], agent_type="general",
        )
    return {"a": a_id, "b": b_id, "b_trace": made["id"], "limiter": limiter}


async def _b_trace_state(session_factory, trace_id):
    async with session_scope(session_factory) as session:
        row = (await session.execute(select(Trace).where(Trace.id == trace_id))).scalar_one_or_none()
        return None if row is None else (row.title, row.tags, row.subject_ids if hasattr(row, "subject_ids") else None)


async def test_foreign_and_hostile_ids_are_not_found_everywhere(session_factory, config, two_orgs):
    a, b_trace, limiter = two_orgs["a"], two_orgs["b_trace"], two_orgs["limiter"]
    before = await _b_trace_state(session_factory, b_trace)
    assert before is not None

    calls = {
        "get_trace": lambda s, t: crud.get_trace(s, a, t),
        "delete_trace": lambda s, t: crud.delete_trace(s, a, t),
        "vote_trace": lambda s, t: crud.vote_trace(s, a, t, "down", "wrong", "x"),
        "tag_trace_subjects": lambda s, t: crud.tag_trace_subjects(s, a, t, ["subject-1"]),
        "amend_trace": lambda s, t: crud.amend_trace(s, a, t, config, limiter, title="hijacked"),
    }
    for name, call in calls.items():
        for trace_id in [b_trace, *HOSTILE]:
            async with session_scope(session_factory) as session:
                result = await call(session, trace_id)
            assert result in (None, False) or (isinstance(result, dict) and result.get("error")), (
                name, trace_id[:40], result,
            )
    assert await _b_trace_state(session_factory, b_trace) == before
