"""hub/crud.py: tag_trace_subjects / find_traces_by_subject /
purge_traces_by_subject -- the structured half of audit 2.2's subject-
erasure gap. `search_trace_content` (b4d9e12a6f37's predecessor) closed
"no field this system could search on"; this closes "cannot honestly
claim provably complete" for content a curator has explicitly tagged.

What these tests defend, in order of how badly getting it wrong would
hurt:

1. **Tagging is a REPLACE, not an append** -- a retried or corrected tag
   call must not accumulate duplicates or leave a stale subject behind.
2. **The match is exact**, not stemmed or fuzzy: a subject_id that is a
   substring or a stemmed variant of a tagged one must NOT match.
3. **Purge deletes the whole amendment chain**, the same completeness
   `delete_trace` already gives a single trace -- a subject's content can
   persist across a supersession even where only one revision was tagged.
4. **Tenancy**: a subject_id tagged in one org is invisible to, and
   unpurgeable by, another.
5. **Validation refuses, never silently truncates or drops**, an
   oversized or malformed tag -- a silently dropped id is a subject this
   trace would then fail to be found under later.
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

from hub import auth, crud
from hub import scopes as scopes_module
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace
from hub.server import build_mcp_server

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory) -> str:
    async with session_scope(session_factory) as session:
        o = Organization(name="subject-tag-org")
        session.add(o)
        await session.flush()
        return o.id


@pytest_asyncio.fixture
async def other_org(session_factory) -> str:
    async with session_scope(session_factory) as session:
        o = Organization(name="other-subject-tag-org")
        session.add(o)
        await session.flush()
        return o.id


@pytest_asyncio.fixture
async def mcp(config, session_factory):
    return build_mcp_server(config, session_factory, make_rate_limiter(config))


async def _trace(session_factory, org_id, *, title="t", context="c", solution="s"):
    async with session_scope(session_factory) as session:
        trace = Trace(org_id=org_id, title=title, context_text=context, solution_text=solution, agent_type="support")
        session.add(trace)
        await session.flush()
        return trace.id


class TestTagging:
    async def test_tag_then_find_exact_match(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        async with session_scope(session_factory) as session:
            tagged = await crud.tag_trace_subjects(session, org, trace_id, ["user-42"], actor="t")
        assert tagged["subject_ids"] == ["user-42"]
        async with session_factory() as session:
            found = await crud.find_traces_by_subject(session, org, "user-42")
        assert [f["id"] for f in found] == [trace_id]

    async def test_tagging_replaces_not_appends(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        async with session_scope(session_factory) as session:
            await crud.tag_trace_subjects(session, org, trace_id, ["user-1"], actor="t")
        async with session_scope(session_factory) as session:
            result = await crud.tag_trace_subjects(session, org, trace_id, ["user-2"], actor="t")
        assert result["subject_ids"] == ["user-2"]
        async with session_factory() as session:
            assert await crud.find_traces_by_subject(session, org, "user-1") == []

    async def test_tagging_with_an_empty_list_clears_it(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        async with session_scope(session_factory) as session:
            await crud.tag_trace_subjects(session, org, trace_id, ["user-1"], actor="t")
        async with session_scope(session_factory) as session:
            result = await crud.tag_trace_subjects(session, org, trace_id, [], actor="t")
        assert result["subject_ids"] == []

    async def test_duplicates_in_the_input_are_deduplicated(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        async with session_scope(session_factory) as session:
            result = await crud.tag_trace_subjects(
                session, org, trace_id, ["user-1", "user-1", "user-2"], actor="t",
            )
        assert result["subject_ids"] == ["user-1", "user-2"]

    async def test_tagging_a_nonexistent_trace_returns_none(self, session_factory, org):
        async with session_scope(session_factory) as session:
            result = await crud.tag_trace_subjects(
                session, org, "00000000-0000-0000-0000-000000000000", ["x"], actor="t",
            )
        assert result is None

    async def test_tagging_a_non_uuid_shaped_id_returns_none_not_a_crash(self, session_factory, org):
        async with session_scope(session_factory) as session:
            result = await crud.tag_trace_subjects(session, org, "not-a-uuid", ["x"], actor="t")
        assert result is None

    async def test_too_many_subject_ids_is_refused(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        too_many = [f"user-{i}" for i in range(crud.MAX_SUBJECT_IDS_PER_TRACE + 1)]
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="too many subject_ids"):
                await crud.tag_trace_subjects(session, org, trace_id, too_many, actor="t")

    async def test_an_oversized_subject_id_is_refused(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="exceeds"):
                await crud.tag_trace_subjects(
                    session, org, trace_id, ["x" * (crud.MAX_SUBJECT_ID_CHARS + 1)], actor="t",
                )


class TestExactMatchIsNotFuzzy:
    async def test_a_substring_of_a_tagged_id_does_not_match(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        async with session_scope(session_factory) as session:
            await crud.tag_trace_subjects(session, org, trace_id, ["user-42"], actor="t")
        async with session_factory() as session:
            assert await crud.find_traces_by_subject(session, org, "user-4") == []
            assert await crud.find_traces_by_subject(session, org, "42") == []

    async def test_untagged_free_text_naming_the_subject_is_not_found(self, session_factory, org):
        """The whole point of the distinction from search_trace_content:
        this function ONLY ever returns explicitly tagged traces."""
        await _trace(session_factory, org, context="this concerns user-42 directly")
        async with session_factory() as session:
            assert await crud.find_traces_by_subject(session, org, "user-42") == []


class TestTenancy:
    async def test_a_tag_in_one_org_is_invisible_to_another(self, session_factory, org, other_org):
        trace_id = await _trace(session_factory, org)
        async with session_scope(session_factory) as session:
            await crud.tag_trace_subjects(session, org, trace_id, ["user-42"], actor="t")
        async with session_factory() as session:
            assert await crud.find_traces_by_subject(session, other_org, "user-42") == []

    async def test_a_trace_cannot_be_tagged_through_another_orgs_id(self, session_factory, org, other_org):
        trace_id = await _trace(session_factory, org)
        async with session_scope(session_factory) as session:
            result = await crud.tag_trace_subjects(session, other_org, trace_id, ["user-42"], actor="t")
        assert result is None

    async def test_purge_never_reaches_another_orgs_tagged_trace(self, session_factory, org, other_org):
        trace_id = await _trace(session_factory, org)
        async with session_scope(session_factory) as session:
            await crud.tag_trace_subjects(session, org, trace_id, ["user-42"], actor="t")
        async with session_scope(session_factory) as session:
            result = await crud.purge_traces_by_subject(session, other_org, "user-42", actor="t")
        assert result == {"purged": 0, "ids": []}
        async with session_factory() as session:
            still_there = await session.get(Trace, trace_id)
        assert still_there is not None


class TestPurge:
    async def test_purge_deletes_the_tagged_trace(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        async with session_scope(session_factory) as session:
            await crud.tag_trace_subjects(session, org, trace_id, ["user-42"], actor="t")
        async with session_scope(session_factory) as session:
            result = await crud.purge_traces_by_subject(session, org, "user-42", actor="t")
        assert result["purged"] == 1
        assert result["ids"] == [trace_id]
        async with session_factory() as session:
            assert await session.get(Trace, trace_id) is None

    async def test_purge_deletes_the_whole_amendment_chain(self, session_factory, org, config):
        """Content about a subject can persist across a supersession even
        where only one revision in the chain was explicitly tagged --
        purge_traces_by_subject delegates to delete_trace precisely so
        the whole chain goes, not just the tagged revision."""
        original_id = await _trace(session_factory, org, title="original")
        async with session_scope(session_factory) as session:
            await crud.tag_trace_subjects(session, org, original_id, ["user-42"], actor="t")
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            amended = await crud.amend_trace(
                session, org, original_id, config, rate_limiter, title="amended", actor="t",
            )
        amended_id = amended["id"]
        async with session_scope(session_factory) as session:
            result = await crud.purge_traces_by_subject(session, org, "user-42", actor="t")
        assert result["purged"] == 2
        assert set(result["ids"]) == {original_id, amended_id}

    async def test_a_subject_id_nothing_was_tagged_with_purges_nothing(self, session_factory, org):
        async with session_scope(session_factory) as session:
            result = await crud.purge_traces_by_subject(session, org, "no-such-subject", actor="t")
        assert result == {"purged": 0, "ids": []}

    async def test_purge_decrements_the_org_trace_count(self, session_factory, org, config):
        """Via contribute_trace, not the lightweight _trace() helper this
        file otherwise uses -- trace_count is only ever incremented on
        that real write path, and this is the one test that checks it."""
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            contributed = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s", actor="t",
            )
        trace_id = contributed["id"]
        async with session_scope(session_factory) as session:
            await crud.tag_trace_subjects(session, org, trace_id, ["user-42"], actor="t")
        async with session_factory() as session:
            before = (await session.get(Organization, org)).trace_count
        async with session_scope(session_factory) as session:
            await crud.purge_traces_by_subject(session, org, "user-42", actor="t")
        async with session_factory() as session:
            after = (await session.get(Organization, org)).trace_count
        assert after == before - 1


def _payload(result) -> dict:
    if getattr(result, "structured_content", None):
        sc = result.structured_content
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


class _Scoped:
    def __init__(self, org_id: str, scopes):
        self._org_id = org_id
        self._scopes = scopes

    def __enter__(self):
        self._org = auth.current_org_id.set(self._org_id)
        self._actor = auth.current_actor.set("ct_live_test")
        self._scope_token = auth.current_scopes.set(self._scopes)
        return self

    def __exit__(self, *exc):
        auth.current_scopes.reset(self._scope_token)
        auth.current_actor.reset(self._actor)
        auth.current_org_id.reset(self._org)
        return False


class TestToolsEndToEnd:
    async def test_tag_find_purge_round_trip_through_the_mcp_tools(self, mcp, session_factory, org):
        trace_id = await _trace(session_factory, org)

        with _Scoped(org, (scopes_module.SCOPE_WRITE,)):
            tagged = _payload(await mcp.call_tool(
                "tag_trace_subjects", {"id": trace_id, "subject_ids": ["user-42"]},
            ))
        assert tagged["subject_ids"] == ["user-42"]

        with _Scoped(org, (scopes_module.SCOPE_READ,)):
            found = _payload(await mcp.call_tool(
                "find_traces_by_subject", {"subject_id": "user-42"},
            ))
        assert [r["id"] for r in found["results"]] == [trace_id]

        with _Scoped(org, (scopes_module.SCOPE_ADMIN,)):
            purged = _payload(await mcp.call_tool(
                "purge_traces_by_subject", {"subject_id": "user-42"},
            ))
        assert purged == {"purged": 1, "ids": [trace_id]}

    async def test_tag_trace_subjects_requires_write_not_read(self, mcp, session_factory, org):
        trace_id = await _trace(session_factory, org)
        with _Scoped(org, (scopes_module.SCOPE_READ,)):
            result = _payload(await mcp.call_tool(
                "tag_trace_subjects", {"id": trace_id, "subject_ids": ["user-42"]},
            ))
        assert result["error"] == "forbidden"

    async def test_purge_traces_by_subject_requires_admin_not_write(self, mcp, session_factory, org):
        trace_id = await _trace(session_factory, org)
        with _Scoped(org, (scopes_module.SCOPE_WRITE,)):
            await mcp.call_tool("tag_trace_subjects", {"id": trace_id, "subject_ids": ["user-42"]})
        with _Scoped(org, (scopes_module.SCOPE_WRITE,)):
            result = _payload(await mcp.call_tool(
                "purge_traces_by_subject", {"subject_id": "user-42"},
            ))
        assert result["error"] == "forbidden"
