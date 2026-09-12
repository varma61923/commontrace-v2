"""hub/crud.py:search_trace_content -- audit 2.2's "a customer who needs
subject-level erasure over trace content must locate the traces
themselves; there is no field this system could search on to do it for
them." This is the tool that locates them.

What these tests defend, in order of how badly getting it wrong would
hurt:

1. **It finds an exact identifier `search_traces` could miss.** Stemming
   is right for relevance search and wrong for an exact subject
   identifier -- this function must not stem, tokenize, or rank.
2. **Only one regex engine is ever consulted.** Postgres validates and
   matches `regex=True` patterns end to end; there is no separate
   Python-side `re` pass that could disagree with it or reject a pattern
   Postgres would have accepted (see the function's own docstring).
3. **Quarantined traces are still findable** -- unlike `search_traces`,
   which deliberately excludes them. A quarantined trace still contains
   whatever a subject-erasure request is looking for.
4. **Tenancy**: never a hit across an org boundary.
5. **A non-match is never mistaken for proof of absence** by anything
   this function returns -- there is no "0 results" claim beyond "0
   results for this exact pattern."
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from hub import auth, crud
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace
from hub.server import build_mcp_server

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory) -> str:
    async with session_scope(session_factory) as session:
        o = Organization(name="content-search-org")
        session.add(o)
        await session.flush()
        return o.id


@pytest_asyncio.fixture
async def other_org(session_factory) -> str:
    async with session_scope(session_factory) as session:
        o = Organization(name="other-content-search-org")
        session.add(o)
        await session.flush()
        return o.id


async def _trace(session_factory, org_id, *, title="t", context="c", solution="s", quarantined=False):
    async with session_scope(session_factory) as session:
        trace = Trace(
            org_id=org_id, title=title, context_text=context, solution_text=solution,
            agent_type="support", quarantined=quarantined,
        )
        session.add(trace)
        await session.flush()
        return trace.id


class TestLiteralMatch:
    async def test_finds_an_exact_identifier_in_context_text(self, session_factory, org):
        await _trace(session_factory, org, context="reported by jane.smith@example.com yesterday")
        async with session_scope(session_factory) as session:
            results = await crud.search_trace_content(session, org, "jane.smith@example.com")
        assert len(results) == 1
        assert results[0]["matched_field"] == "context_text"
        assert "jane.smith@example.com" in results[0]["snippet"]

    async def test_is_case_insensitive(self, session_factory, org):
        await _trace(session_factory, org, solution="Escalated to Jane Smith")
        async with session_scope(session_factory) as session:
            results = await crud.search_trace_content(session, org, "jane smith")
        assert len(results) == 1
        assert results[0]["matched_field"] == "solution_text"

    async def test_no_match_returns_empty_not_an_error(self, session_factory, org):
        await _trace(session_factory, org, context="nothing relevant here")
        async with session_scope(session_factory) as session:
            results = await crud.search_trace_content(session, org, "no-such-identifier@example.com")
        assert results == []

    async def test_matches_title_when_that_is_where_the_identifier_is(self, session_factory, org):
        await _trace(session_factory, org, title="Complaint from bob@example.com", context="c", solution="s")
        async with session_scope(session_factory) as session:
            results = await crud.search_trace_content(session, org, "bob@example.com")
        assert results[0]["matched_field"] == "title"

    async def test_a_quarantined_trace_is_still_found(self, session_factory, org):
        """Unlike search_traces, which excludes quarantined traces --
        a quarantined trace still contains whatever an erasure request
        is looking for."""
        await _trace(session_factory, org, context="jane.smith@example.com", quarantined=True)
        async with session_scope(session_factory) as session:
            results = await crud.search_trace_content(session, org, "jane.smith@example.com")
        assert len(results) == 1
        assert results[0]["quarantined"] is True

    async def test_never_matches_across_an_org_boundary(self, session_factory, org, other_org):
        await _trace(session_factory, other_org, context="jane.smith@example.com")
        async with session_scope(session_factory) as session:
            results = await crud.search_trace_content(session, org, "jane.smith@example.com")
        assert results == []

    async def test_results_are_bounded_by_limit(self, session_factory, org):
        for _ in range(5):
            await _trace(session_factory, org, context="jane.smith@example.com")
        async with session_scope(session_factory) as session:
            results = await crud.search_trace_content(session, org, "jane.smith@example.com", limit=2)
        assert len(results) == 2

    async def test_the_snippet_is_bounded_not_the_whole_field(self, session_factory, org):
        long_text = ("padding " * 100) + "jane.smith@example.com" + (" padding" * 100)
        await _trace(session_factory, org, context=long_text)
        async with session_scope(session_factory) as session:
            results = await crud.search_trace_content(session, org, "jane.smith@example.com")
        snippet = results[0]["snippet"]
        assert "jane.smith@example.com" in snippet
        assert len(snippet) < len(long_text)


class TestRegexMatch:
    async def test_a_valid_pattern_matches(self, session_factory, org):
        await _trace(session_factory, org, context="ticket #48213 escalated")
        async with session_scope(session_factory) as session:
            results = await crud.search_trace_content(session, org, r"#\d{5}", regex=True)
        assert len(results) == 1
        assert results[0]["matched_field"] == "context_text"

    async def test_an_invalid_pattern_is_a_clean_value_error_not_a_500(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="not a valid regular expression"):
                await crud.search_trace_content(session, org, "(unbalanced(", regex=True)

    async def test_regex_mode_is_case_insensitive_like_literal_mode(self, session_factory, org):
        await _trace(session_factory, org, solution="Escalated to JANE SMITH")
        async with session_scope(session_factory) as session:
            results = await crud.search_trace_content(session, org, "jane smith", regex=True)
        assert len(results) == 1

    async def test_a_syntactically_valid_but_non_matching_pattern_finds_nothing(
        self, session_factory, org
    ):
        await _trace(session_factory, org, context="no numbers here")
        async with session_scope(session_factory) as session:
            results = await crud.search_trace_content(session, org, r"\d{5}", regex=True)
        assert results == []


class TestMatchedFieldPrecedence:
    async def test_title_wins_when_multiple_fields_match(self, session_factory, org):
        await _trace(
            session_factory, org,
            title="jane.smith@example.com escalation",
            context="also mentions jane.smith@example.com",
            solution="s",
        )
        async with session_scope(session_factory) as session:
            results = await crud.search_trace_content(session, org, "jane.smith@example.com")
        assert results[0]["matched_field"] == "title"


# --- through a real MCP tool call ---------------------------------------------

@pytest_asyncio.fixture
async def mcp(config, session_factory):
    return build_mcp_server(config, session_factory, make_rate_limiter(config))


def _payload(result) -> dict:
    if getattr(result, "structured_content", None):
        sc = result.structured_content
        return sc.get("result", sc)
    import json
    return json.loads(result.content[0].text)


class _Scoped:
    """Same idiom as hub/tests/test_api_key_scopes.py's own helper: set
    the contextvars a real API-key request's middleware would set."""

    def __init__(self, org_id: str, scopes: tuple[str, ...]):
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


class TestToolEndToEnd:
    async def test_a_read_scoped_key_can_call_it(self, mcp, session_factory, org):
        from hub import scopes as scopes_module

        await _trace(session_factory, org, context="jane.smith@example.com")
        with _Scoped(org, (scopes_module.SCOPE_READ,)):
            result = _payload(await mcp.call_tool(
                "search_trace_content", {"pattern": "jane.smith@example.com"},
            ))
        assert len(result["results"]) == 1

    async def test_an_invalid_regex_reports_invalid_request_not_a_crash(
        self, mcp, session_factory, org
    ):
        from hub import scopes as scopes_module

        with _Scoped(org, (scopes_module.SCOPE_READ,)):
            result = _payload(await mcp.call_tool(
                "search_trace_content", {"pattern": "(unbalanced(", "regex": True},
            ))
        assert result["error"] == "invalid_request"
