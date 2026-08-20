"""The single most important test file in the Hub server.

Creates two orgs with overlapping trace content, calls every one of the six
Hub tools as org_a, and asserts zero rows belonging to org_b ever appear in
any response -- and that get_trace on a known org_b trace id reports
not_found, never a permission error (never confirming the id exists).

This exercises hub/crud.py directly rather than driving a live MCP
transport. That is deliberate, not a shortcut: per hub/server.py's module
docstring, the MCP tool wrappers do nothing but resolve org_id from request
context and call these exact crud functions -- there is no additional
scoping logic at the transport layer to test separately. Testing crud.py
directly is testing the real org-scoping code path, and it lets this test
assert on the query layer without needing a running HTTP server.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from hub import auth, crud
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def two_orgs_with_overlapping_content(session_factory, config):
    """org_a and org_b each contribute a trace with the *same* title/tags,
    so a query-layer bug that forgets the org_id filter (e.g. matching on
    title/tags alone) would be caught, not just a bug that returns
    literally everything."""
    rate_limiter = make_rate_limiter(config)

    async with session_scope(session_factory) as session:
        org_a = Organization(name="org-a")
        org_b = Organization(name="org-b")
        session.add_all([org_a, org_b])
        await session.flush()
        org_a_id, org_b_id = org_a.id, org_b.id

    async with session_scope(session_factory) as session:
        a_result = await crud.contribute_trace(
            session,
            org_a_id,
            config,
            rate_limiter,
            title="shared incident title",
            context_text="org A's private context: internal deploy key rotation failed",
            solution_text="org A's private solution: rotate via the break-glass runbook",
            tags=["deploy", "shared-tag"],
            agent_type="code",
        )
        b_result = await crud.contribute_trace(
            session,
            org_b_id,
            config,
            rate_limiter,
            title="shared incident title",
            context_text="org B's private context: customer PII was logged by mistake",
            solution_text="org B's private solution: purge logs and notify DPO",
            tags=["deploy", "shared-tag"],
            agent_type="code",
        )

    return {
        "org_a_id": org_a_id,
        "org_b_id": org_b_id,
        "org_a_trace_id": a_result["id"],
        "org_b_trace_id": b_result["id"],
        "rate_limiter": rate_limiter,
    }


def _assert_no_org_b_leakage(payload, org_b_trace_id: str, org_b_id: str):
    """Walk a tool's return value (a dict or list of dicts) and assert
    nothing in it is org_b's trace id, nor contains org_b's private text."""
    blob = str(payload)
    assert org_b_trace_id not in blob, f"org_b trace id leaked into response: {payload!r}"
    assert org_b_id not in blob, f"org_b org id leaked into response: {payload!r}"
    assert "customer PII" not in blob, f"org_b's private content leaked into response: {payload!r}"
    assert "DPO" not in blob, f"org_b's private content leaked into response: {payload!r}"


async def test_search_traces_excludes_other_org(session_factory, config, two_orgs_with_overlapping_content):
    fixture = two_orgs_with_overlapping_content
    async with session_scope(session_factory) as session:
        page = await crud.search_traces(session, fixture["org_a_id"], query="shared incident")
    results = page["traces"]

    assert len(results) == 1
    assert results[0]["id"] == fixture["org_a_trace_id"]
    _assert_no_org_b_leakage(results, fixture["org_b_trace_id"], fixture["org_b_id"])


async def test_search_traces_by_shared_tag_excludes_other_org(
    session_factory, config, two_orgs_with_overlapping_content
):
    fixture = two_orgs_with_overlapping_content
    async with session_scope(session_factory) as session:
        page = await crud.search_traces(session, fixture["org_a_id"], tags=["shared-tag"])
    results = page["traces"]

    assert len(results) == 1
    assert results[0]["id"] == fixture["org_a_trace_id"]
    _assert_no_org_b_leakage(results, fixture["org_b_trace_id"], fixture["org_b_id"])


async def test_contribute_trace_is_scoped_to_calling_org(
    session_factory, config, two_orgs_with_overlapping_content
):
    fixture = two_orgs_with_overlapping_content
    async with session_scope(session_factory) as session:
        # org_a can see its own newly-contributed trace...
        own = await crud.get_trace(session, fixture["org_a_id"], fixture["org_a_trace_id"])
        assert own is not None
        # ...but never org_b's, even though it was contributed in the same fixture.
        other = await crud.get_trace(session, fixture["org_a_id"], fixture["org_b_trace_id"])
    assert other is None


async def test_get_trace_on_other_orgs_id_is_not_found_not_forbidden(
    session_factory, config, two_orgs_with_overlapping_content
):
    """The specific requirement from the brief: a known org_b trace id,
    fetched as org_a, must come back not-found -- indistinguishable from an
    id that never existed at all, not a 403-shaped 'yes it exists, no you
    can't see it' response that would confirm the id is real."""
    fixture = two_orgs_with_overlapping_content
    async with session_scope(session_factory) as session:
        result = await crud.get_trace(session, fixture["org_a_id"], fixture["org_b_trace_id"])
    assert result is None

    async with session_scope(session_factory) as session:
        # A trace id that never existed at all must look identical.
        never_existed = await crud.get_trace(session, fixture["org_a_id"], "00000000-0000-0000-0000-000000000000")
    assert never_existed is None


async def test_vote_trace_cannot_target_other_orgs_trace(
    session_factory, config, two_orgs_with_overlapping_content
):
    fixture = two_orgs_with_overlapping_content
    async with session_scope(session_factory) as session:
        result = await crud.vote_trace(session, fixture["org_a_id"], fixture["org_b_trace_id"], "up")
    assert result is None

    # org_b's trace is unaffected -- its trust score is still the neutral default.
    async with session_scope(session_factory) as session:
        untouched = await crud.get_trace(session, fixture["org_b_id"], fixture["org_b_trace_id"])
    assert untouched["trust"] == 0.5


async def test_amend_trace_cannot_target_other_orgs_trace(
    session_factory, config, two_orgs_with_overlapping_content
):
    fixture = two_orgs_with_overlapping_content
    async with session_scope(session_factory) as session:
        result = await crud.amend_trace(
            session, fixture["org_a_id"], fixture["org_b_trace_id"],
            config, make_rate_limiter(config), title="hijacked",
        )
    assert result is None

    async with session_scope(session_factory) as session:
        untouched = await crud.get_trace(session, fixture["org_b_id"], fixture["org_b_trace_id"])
    assert untouched["title"] == "shared incident title"


async def test_list_tags_excludes_other_orgs_tags(session_factory, config, two_orgs_with_overlapping_content):
    fixture = two_orgs_with_overlapping_content
    async with session_scope(session_factory) as session:
        # give org_a an exclusive tag and org_b a *different* exclusive tag
        await crud.contribute_trace(
            session,
            fixture["org_a_id"],
            config,
            fixture["rate_limiter"],
            title="org a only",
            context_text="context",
            solution_text="solution",
            tags=["org-a-exclusive-tag"],
            agent_type="code",
        )
        await crud.contribute_trace(
            session,
            fixture["org_b_id"],
            config,
            fixture["rate_limiter"],
            title="org b only",
            context_text="context",
            solution_text="solution",
            tags=["org-b-exclusive-tag"],
            agent_type="code",
        )

    async with session_scope(session_factory) as session:
        org_a_tags = await crud.list_tags(session, fixture["org_a_id"])

    assert "org-a-exclusive-tag" in org_a_tags
    assert "org-b-exclusive-tag" not in org_a_tags


async def test_all_six_tools_as_org_a_never_return_org_b_rows(
    session_factory, config, two_orgs_with_overlapping_content
):
    """Runs every one of the six Hub tools as org_a and asserts none of
    org_b's identifiers or private content ever appear in any response --
    the exact assertion the brief specifies."""
    fixture = two_orgs_with_overlapping_content
    org_a, org_b_trace_id, org_b_id = fixture["org_a_id"], fixture["org_b_trace_id"], fixture["org_b_id"]

    async with session_scope(session_factory) as session:
        search_result = await crud.search_traces(session, org_a, query="")
    _assert_no_org_b_leakage(search_result, org_b_trace_id, org_b_id)  # walks the whole payload

    async with session_scope(session_factory) as session:
        contribute_result = await crud.contribute_trace(
            session,
            org_a,
            config,
            fixture["rate_limiter"],
            title="org a new trace",
            context_text="context",
            solution_text="solution",
            tags=[],
            agent_type="code",
        )
    _assert_no_org_b_leakage(contribute_result, org_b_trace_id, org_b_id)

    async with session_scope(session_factory) as session:
        get_result = await crud.get_trace(session, org_a, org_b_trace_id)
    assert get_result is None

    async with session_scope(session_factory) as session:
        vote_result = await crud.vote_trace(session, org_a, org_b_trace_id, "up")
    assert vote_result is None

    async with session_scope(session_factory) as session:
        amend_result = await crud.amend_trace(
            session, org_a, org_b_trace_id, config, make_rate_limiter(config), title="x"
        )
    assert amend_result is None

    async with session_scope(session_factory) as session:
        tags_result = await crud.list_tags(session, org_a)
    _assert_no_org_b_leakage(tags_result, org_b_trace_id, org_b_id)


async def test_quarantined_traces_still_scoped_to_owning_org(session_factory, config):
    """A quarantined trace is excluded from search_traces/list_tags for
    everyone -- including its own org -- but that exclusion must never be
    confused with cross-org leakage: quarantine is a content-moderation
    state, org_id scoping is a security boundary, and they're independent
    axes tested independently here."""
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        org = Organization(name="spammy-org")
        session.add(org)
        await session.flush()
        org_id = org.id

    spam_urls = " ".join(f"http://spam{i}.example.com" for i in range(config.suspect_url_threshold + 1))
    async with session_scope(session_factory) as session:
        result = await crud.contribute_trace(
            session,
            org_id,
            config,
            rate_limiter,
            title="spammy trace",
            context_text=spam_urls,
            solution_text="solution",
            tags=[],
            agent_type="code",
        )
    assert result["quarantined"] is True

    async with session_scope(session_factory) as session:
        search_results = await crud.search_traces(session, org_id, query="spammy")
    assert search_results["traces"] == []


async def test_org_identity_is_per_request_not_per_session(session_factory, config):
    """A review raised that org identity might be bound at MCP *session*
    creation rather than per request -- if tool handlers ran in a long-lived
    task whose context was copied at spawn, `get_current_org_id()` would
    return the initialize request's org forever, and any valid key plus
    another org's `mcp-session-id` would read that org's traces.

    Driven against a live server it does NOT reproduce: the contextvar the
    middleware sets is the one the handler observes, including under
    concurrent interleaved requests from two orgs. But the safety of that
    depends on how the MCP SDK schedules handlers, which is an implicit
    dependency on a library internal that an upgrade could change silently.

    So this pins the property directly at the layer that matters: whatever the
    contextvar says when a crud call runs is the org it operates on, and two
    interleaved callers never observe each other's value. If an SDK upgrade
    ever breaks the request-scoping, this fails instead of a customer finding
    out.
    """
    rate_limiter = make_rate_limiter(config)

    async with session_scope(session_factory) as session:
        org_a = Organization(name="ctx-org-a")
        org_b = Organization(name="ctx-org-b")
        session.add_all([org_a, org_b])
        await session.flush()
        org_a_id, org_b_id = str(org_a.id), str(org_b.id)

    async with session_scope(session_factory) as session:
        a_trace = await crud.contribute_trace(
            session, org_a_id, config, rate_limiter,
            title="ctx-a-secret", context_text="org a only", solution_text="s", tags=["ctx-a"],
        )
    a_trace_id = a_trace["id"]

    observed: list[tuple[str, str | None]] = []

    async def act_as(org_id: str, label: str) -> None:
        """Set the contextvar, yield to the loop, then read it back."""
        token = auth.current_org_id.set(org_id)
        try:
            await asyncio.sleep(0)  # force interleaving with the other task
            seen = auth.get_current_org_id()
            observed.append((label, seen))
            async with session_scope(session_factory) as session:
                # The org the handler acts on must be the one IT set, never
                # whatever a concurrently-running caller set.
                result = await crud.get_trace(session, seen, a_trace_id)
            if label == "a":
                assert result is not None and result["title"] == "ctx-a-secret"
            else:
                assert result is None, "org B observed org A's trace"
        finally:
            auth.current_org_id.reset(token)

    await asyncio.gather(
        *[act_as(org_a_id, "a") for _ in range(20)],
        *[act_as(org_b_id, "b") for _ in range(20)],
    )

    for label, seen in observed:
        expected = org_a_id if label == "a" else org_b_id
        assert seen == expected, f"caller {label} observed org {seen}, expected {expected}"
