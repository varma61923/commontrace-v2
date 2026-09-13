"""Collaboration on a trace, for a customer's own team (audit §8.1):
comments, assignment, and a notification inbox (hub/collab.py).

Distinct from hub/manage.py's Knowledge Base review queue, which is an
operator surface across tenants -- this is a customer's own team working
on their own traces, and only exists now that hub/models.py:User gives a
request an actual PERSON to attribute a comment to or assign work to.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from hub import auth, collab, rbac
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace, User
from hub.server import build_mcp_server

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory) -> str:
    async with session_scope(session_factory) as session:
        organization = Organization(name="collab-org")
        session.add(organization)
        await session.flush()
        return organization.id


@pytest_asyncio.fixture
async def other_org(session_factory) -> str:
    async with session_scope(session_factory) as session:
        organization = Organization(name="other-org")
        session.add(organization)
        await session.flush()
        return organization.id


async def _trace(session_factory, org_id) -> str:
    async with session_scope(session_factory) as session:
        trace = Trace(
            org_id=org_id, title="t", context_text="c", solution_text="s",
            agent_type="support",
        )
        session.add(trace)
        await session.flush()
        return trace.id


async def _user(session_factory, org_id, role=rbac.ROLE_CURATOR, email="p@x.test", disabled=False):
    async with session_scope(session_factory) as session:
        from datetime import datetime, timezone

        user = User(org_id=org_id, email=email, role=role, created_by="test")
        if disabled:
            user.disabled_at = datetime.now(timezone.utc)
        session.add(user)
        await session.flush()
        return user.id


def _as_authenticated(user_id: str, org_id: str, role: str, email: str) -> auth.AuthenticatedUser:
    return auth.AuthenticatedUser(id=user_id, org_id=org_id, role=role, email=email)


class _AsUser:
    """Same idiom as hub/tests/test_user_identity.py's `_AsUser` /
    test_api_key_scopes.py's `_Scoped`: set the contextvars a real
    request's middleware would set, without standing up an HTTP layer."""

    def __init__(self, org_id: str, person: auth.AuthenticatedUser):
        self._org_id = org_id
        self._person = person

    def __enter__(self):
        self._org = auth.current_org_id.set(self._org_id)
        self._actor = auth.current_actor.set(f"user:{self._person.id}")
        self._scopes = auth.current_scopes.set(rbac.scopes_of(self._person.role))
        self._user = auth.current_user.set(self._person)
        return self

    def __exit__(self, *exc):
        auth.current_user.reset(self._user)
        auth.current_scopes.reset(self._scopes)
        auth.current_actor.reset(self._actor)
        auth.current_org_id.reset(self._org)
        return False


# --- hub/collab.py directly ---------------------------------------------------

class TestAddComment:
    async def test_adding_a_comment_returns_the_author_and_body(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        author_id = await _user(session_factory, org, email="a@x.test")
        author = _as_authenticated(author_id, org, rbac.ROLE_CURATOR, "a@x.test")
        async with session_scope(session_factory) as session:
            result = await collab.add_comment(session, org, author, trace_id, "  looks good  ")
        assert result["author_email"] == "a@x.test"
        assert result["body"] == "looks good"

    async def test_an_empty_comment_is_refused(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        author_id = await _user(session_factory, org)
        author = _as_authenticated(author_id, org, rbac.ROLE_CURATOR, "p@x.test")
        async with session_scope(session_factory) as session:
            with pytest.raises(collab.CollabError):
                await collab.add_comment(session, org, author, trace_id, "   ")

    async def test_an_oversized_comment_is_refused(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        author_id = await _user(session_factory, org)
        author = _as_authenticated(author_id, org, rbac.ROLE_CURATOR, "p@x.test")
        async with session_scope(session_factory) as session:
            with pytest.raises(collab.CollabError):
                await collab.add_comment(session, org, author, trace_id, "x" * 4001)

    async def test_commenting_on_an_unknown_trace_is_not_found(self, session_factory, org):
        author_id = await _user(session_factory, org)
        author = _as_authenticated(author_id, org, rbac.ROLE_CURATOR, "p@x.test")
        async with session_scope(session_factory) as session:
            with pytest.raises(collab.CollabNotFound):
                await collab.add_comment(
                    session, org, author, "00000000-0000-0000-0000-000000000000", "hi",
                )

    async def test_commenting_on_another_orgs_trace_is_not_found(
        self, session_factory, org, other_org
    ):
        trace_id = await _trace(session_factory, other_org)
        author_id = await _user(session_factory, org)
        author = _as_authenticated(author_id, org, rbac.ROLE_CURATOR, "p@x.test")
        async with session_scope(session_factory) as session:
            with pytest.raises(collab.CollabNotFound):
                await collab.add_comment(session, org, author, trace_id, "hi")

    async def test_a_comment_notifies_the_current_assignee_not_the_author(
        self, session_factory, org
    ):
        trace_id = await _trace(session_factory, org)
        assignee_id = await _user(session_factory, org, email="assignee@x.test")
        author_id = await _user(session_factory, org, email="author@x.test")
        assignee = _as_authenticated(assignee_id, org, rbac.ROLE_CURATOR, "assignee@x.test")
        author = _as_authenticated(author_id, org, rbac.ROLE_CURATOR, "author@x.test")
        async with session_scope(session_factory) as session:
            await collab.assign(session, org, assignee, trace_id, assignee_id)
        async with session_scope(session_factory) as session:
            await collab.add_comment(session, org, author, trace_id, "please check")
        async with session_scope(session_factory) as session:
            notes = await collab.list_my_notifications(session, org, assignee)
            author_notes = await collab.list_my_notifications(session, org, author)
        assert len(notes) == 1
        assert notes[0]["kind"] == "comment"
        assert author_notes == []

    async def test_commenting_on_your_own_assigned_trace_notifies_no_one(
        self, session_factory, org
    ):
        trace_id = await _trace(session_factory, org)
        person_id = await _user(session_factory, org)
        person = _as_authenticated(person_id, org, rbac.ROLE_CURATOR, "p@x.test")
        async with session_scope(session_factory) as session:
            await collab.assign(session, org, person, trace_id, person_id)
        async with session_scope(session_factory) as session:
            await collab.add_comment(session, org, person, trace_id, "note to self")
        async with session_scope(session_factory) as session:
            notes = await collab.list_my_notifications(session, org, person)
        assert notes == []


class TestListComments:
    async def test_comments_are_returned_oldest_first(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        author_id = await _user(session_factory, org)
        author = _as_authenticated(author_id, org, rbac.ROLE_CURATOR, "p@x.test")
        async with session_scope(session_factory) as session:
            await collab.add_comment(session, org, author, trace_id, "first")
        async with session_scope(session_factory) as session:
            await collab.add_comment(session, org, author, trace_id, "second")
        async with session_scope(session_factory) as session:
            comments = await collab.list_comments(session, org, trace_id)
        assert [c["body"] for c in comments] == ["first", "second"]

    async def test_listing_comments_on_an_unknown_trace_is_not_found(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(collab.CollabNotFound):
                await collab.list_comments(session, org, "00000000-0000-0000-0000-000000000000")


class TestAssign:
    async def test_assigning_returns_the_assignee_email(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        assigner_id = await _user(session_factory, org, email="mgr@x.test")
        assignee_id = await _user(session_factory, org, email="dev@x.test")
        assigner = _as_authenticated(assigner_id, org, rbac.ROLE_CURATOR, "mgr@x.test")
        async with session_scope(session_factory) as session:
            result = await collab.assign(session, org, assigner, trace_id, assignee_id)
        assert result["assignee_email"] == "dev@x.test"

    async def test_reassigning_replaces_the_previous_assignee(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        assigner_id = await _user(session_factory, org, email="mgr@x.test")
        first_id = await _user(session_factory, org, email="first@x.test")
        second_id = await _user(session_factory, org, email="second@x.test")
        assigner = _as_authenticated(assigner_id, org, rbac.ROLE_CURATOR, "mgr@x.test")
        async with session_scope(session_factory) as session:
            await collab.assign(session, org, assigner, trace_id, first_id)
        async with session_scope(session_factory) as session:
            result = await collab.assign(session, org, assigner, trace_id, second_id)
        assert result["assignee_email"] == "second@x.test"

    async def test_assigning_to_a_disabled_user_is_refused(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        assigner_id = await _user(session_factory, org, email="mgr@x.test")
        disabled_id = await _user(session_factory, org, email="gone@x.test", disabled=True)
        assigner = _as_authenticated(assigner_id, org, rbac.ROLE_CURATOR, "mgr@x.test")
        async with session_scope(session_factory) as session:
            with pytest.raises(collab.CollabError):
                await collab.assign(session, org, assigner, trace_id, disabled_id)

    async def test_assigning_to_a_user_in_another_org_is_not_found(
        self, session_factory, org, other_org
    ):
        trace_id = await _trace(session_factory, org)
        assigner_id = await _user(session_factory, org, email="mgr@x.test")
        outsider_id = await _user(session_factory, other_org, email="outsider@x.test")
        assigner = _as_authenticated(assigner_id, org, rbac.ROLE_CURATOR, "mgr@x.test")
        async with session_scope(session_factory) as session:
            with pytest.raises(collab.CollabNotFound):
                await collab.assign(session, org, assigner, trace_id, outsider_id)

    async def test_unassigning_clears_it(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        assigner_id = await _user(session_factory, org, email="mgr@x.test")
        assignee_id = await _user(session_factory, org, email="dev@x.test")
        assigner = _as_authenticated(assigner_id, org, rbac.ROLE_CURATOR, "mgr@x.test")
        async with session_scope(session_factory) as session:
            await collab.assign(session, org, assigner, trace_id, assignee_id)
        async with session_scope(session_factory) as session:
            cleared = await collab.unassign(session, org, assigner, trace_id)
        assert cleared is True

    async def test_unassigning_with_nothing_assigned_reports_false(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        person_id = await _user(session_factory, org)
        person = _as_authenticated(person_id, org, rbac.ROLE_CURATOR, "p@x.test")
        async with session_scope(session_factory) as session:
            cleared = await collab.unassign(session, org, person, trace_id)
        assert cleared is False

    async def test_assigning_yourself_does_not_notify_yourself(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        person_id = await _user(session_factory, org)
        person = _as_authenticated(person_id, org, rbac.ROLE_CURATOR, "p@x.test")
        async with session_scope(session_factory) as session:
            await collab.assign(session, org, person, trace_id, person_id)
        async with session_scope(session_factory) as session:
            notes = await collab.list_my_notifications(session, org, person)
        assert notes == []


class TestNotifications:
    async def test_unread_only_filters_out_read_ones(self, session_factory, org):
        trace_id = await _trace(session_factory, org)
        assigner_id = await _user(session_factory, org, email="mgr@x.test")
        assignee_id = await _user(session_factory, org, email="dev@x.test")
        assigner = _as_authenticated(assigner_id, org, rbac.ROLE_CURATOR, "mgr@x.test")
        assignee = _as_authenticated(assignee_id, org, rbac.ROLE_CURATOR, "dev@x.test")
        async with session_scope(session_factory) as session:
            await collab.assign(session, org, assigner, trace_id, assignee_id)
        async with session_scope(session_factory) as session:
            notes = await collab.list_my_notifications(session, org, assignee)
        async with session_scope(session_factory) as session:
            assert await collab.mark_notification_read(session, org, assignee, notes[0]["id"])
        async with session_scope(session_factory) as session:
            unread = await collab.list_my_notifications(session, org, assignee, unread_only=True)
        assert unread == []

    async def test_marking_someone_elses_notification_read_fails_closed(
        self, session_factory, org
    ):
        trace_id = await _trace(session_factory, org)
        assigner_id = await _user(session_factory, org, email="mgr@x.test")
        assignee_id = await _user(session_factory, org, email="dev@x.test")
        nosy_id = await _user(session_factory, org, email="nosy@x.test")
        assigner = _as_authenticated(assigner_id, org, rbac.ROLE_CURATOR, "mgr@x.test")
        assignee = _as_authenticated(assignee_id, org, rbac.ROLE_CURATOR, "dev@x.test")
        nosy = _as_authenticated(nosy_id, org, rbac.ROLE_CURATOR, "nosy@x.test")
        async with session_scope(session_factory) as session:
            await collab.assign(session, org, assigner, trace_id, assignee_id)
        async with session_scope(session_factory) as session:
            notes = await collab.list_my_notifications(session, org, assignee)
        async with session_scope(session_factory) as session:
            ok = await collab.mark_notification_read(session, org, nosy, notes[0]["id"])
        assert ok is False
        async with session_scope(session_factory) as session:
            still_unread = await collab.list_my_notifications(session, org, assignee, unread_only=True)
        assert len(still_unread) == 1

    async def test_marking_an_unknown_notification_reports_false(self, session_factory, org):
        person_id = await _user(session_factory, org)
        person = _as_authenticated(person_id, org, rbac.ROLE_CURATOR, "p@x.test")
        async with session_scope(session_factory) as session:
            ok = await collab.mark_notification_read(
                session, org, person, "00000000-0000-0000-0000-000000000000",
            )
        assert ok is False


# --- through real MCP tool calls ----------------------------------------------

@pytest_asyncio.fixture
async def mcp(config, session_factory):
    return build_mcp_server(config, session_factory, make_rate_limiter(config))


def _payload(result) -> dict:
    if getattr(result, "structured_content", None):
        sc = result.structured_content
        return sc.get("result", sc)
    import json
    return json.loads(result.content[0].text)


class TestToolsRequireAPerson:
    """add_comment/assign_trace/unassign_trace/list_my_notifications/
    mark_notification_read all need someone to attribute the action to or
    notify -- an API-key-only request (no signed-in person) is refused,
    distinctly from a scope or capability denial."""

    async def test_add_comment_without_a_person_is_refused(self, mcp, session_factory, org):
        from hub import scopes

        trace_id = await _trace(session_factory, org)
        org_token = auth.current_org_id.set(org)
        actor_token = auth.current_actor.set("ct_live_test")
        scope_token = auth.current_scopes.set(scopes.ALL_SCOPES)
        try:
            result = _payload(await mcp.call_tool("add_comment", {
                "trace_id": trace_id, "body": "hi",
            }))
        finally:
            auth.current_scopes.reset(scope_token)
            auth.current_actor.reset(actor_token)
            auth.current_org_id.reset(org_token)
        assert result["error"] == "person_required"

    async def test_list_my_notifications_without_a_person_is_refused(self, mcp, org):
        from hub import scopes

        org_token = auth.current_org_id.set(org)
        actor_token = auth.current_actor.set("ct_live_test")
        scope_token = auth.current_scopes.set(scopes.ALL_SCOPES)
        try:
            result = _payload(await mcp.call_tool("list_my_notifications", {}))
        finally:
            auth.current_scopes.reset(scope_token)
            auth.current_actor.reset(actor_token)
            auth.current_org_id.reset(org_token)
        assert result["error"] == "person_required"


class TestToolsThroughARealPerson:
    async def test_a_curator_can_comment_and_read_it_back(self, mcp, session_factory, org):
        trace_id = await _trace(session_factory, org)
        person_id = await _user(session_factory, org, email="c@x.test")
        person = _as_authenticated(person_id, org, rbac.ROLE_CURATOR, "c@x.test")
        with _AsUser(org, person):
            add_result = _payload(await mcp.call_tool("add_comment", {
                "trace_id": trace_id, "body": "looks solid",
            }))
            list_result = _payload(await mcp.call_tool("list_comments", {"trace_id": trace_id}))
        assert add_result["body"] == "looks solid"
        assert len(list_result["comments"]) == 1

    async def test_an_analyst_cannot_comment_capability_denied(self, mcp, session_factory, org):
        trace_id = await _trace(session_factory, org)
        person_id = await _user(session_factory, org, role=rbac.ROLE_ANALYST, email="an@x.test")
        person = _as_authenticated(person_id, org, rbac.ROLE_ANALYST, "an@x.test")
        with _AsUser(org, person):
            result = _payload(await mcp.call_tool("add_comment", {
                "trace_id": trace_id, "body": "hi",
            }))
        assert result["error"] == "forbidden"
        assert result["required_capability"] == rbac.CAP_CURATE

    async def test_a_viewer_can_still_list_comments(self, mcp, session_factory, org):
        trace_id = await _trace(session_factory, org)
        curator_id = await _user(session_factory, org, email="c@x.test")
        curator = _as_authenticated(curator_id, org, rbac.ROLE_CURATOR, "c@x.test")
        with _AsUser(org, curator):
            await mcp.call_tool("add_comment", {"trace_id": trace_id, "body": "hi"})

        viewer_id = await _user(session_factory, org, role=rbac.ROLE_VIEWER, email="v@x.test")
        viewer = _as_authenticated(viewer_id, org, rbac.ROLE_VIEWER, "v@x.test")
        with _AsUser(org, viewer):
            result = _payload(await mcp.call_tool("list_comments", {"trace_id": trace_id}))
        assert len(result["comments"]) == 1

    async def test_assign_then_notification_then_mark_read_end_to_end(
        self, mcp, session_factory, org
    ):
        trace_id = await _trace(session_factory, org)
        manager_id = await _user(session_factory, org, email="mgr@x.test")
        assignee_id = await _user(session_factory, org, email="dev@x.test")
        manager = _as_authenticated(manager_id, org, rbac.ROLE_CURATOR, "mgr@x.test")
        assignee = _as_authenticated(assignee_id, org, rbac.ROLE_CURATOR, "dev@x.test")

        with _AsUser(org, manager):
            assign_result = _payload(await mcp.call_tool("assign_trace", {
                "trace_id": trace_id, "user_id": assignee_id,
            }))
        assert assign_result["assignee_email"] == "dev@x.test"

        with _AsUser(org, assignee):
            inbox = _payload(await mcp.call_tool("list_my_notifications", {}))
            assert len(inbox["notifications"]) == 1
            notification_id = inbox["notifications"][0]["id"]
            marked = _payload(await mcp.call_tool(
                "mark_notification_read", {"notification_id": notification_id},
            ))
            assert marked["read"] is True
            unread = _payload(await mcp.call_tool(
                "list_my_notifications", {"unread_only": True},
            ))
        assert unread["notifications"] == []

    async def test_unassign_then_reassigning_clears_prior_assignee(
        self, mcp, session_factory, org
    ):
        trace_id = await _trace(session_factory, org)
        manager_id = await _user(session_factory, org, email="mgr@x.test")
        manager = _as_authenticated(manager_id, org, rbac.ROLE_CURATOR, "mgr@x.test")
        with _AsUser(org, manager):
            await mcp.call_tool("assign_trace", {"trace_id": trace_id, "user_id": manager_id})
            result = _payload(await mcp.call_tool("unassign_trace", {"trace_id": trace_id}))
        assert result["cleared"] is True

    async def test_commenting_on_an_unknown_trace_is_not_found_through_the_tool(
        self, mcp, session_factory, org
    ):
        person_id = await _user(session_factory, org)
        person = _as_authenticated(person_id, org, rbac.ROLE_CURATOR, "p@x.test")
        with _AsUser(org, person):
            result = _payload(await mcp.call_tool("add_comment", {
                "trace_id": "00000000-0000-0000-0000-000000000000", "body": "hi",
            }))
        assert result["error"] == "not_found"


class TestEveryCollabToolHasACapabilityMapping:
    async def test_registered_in_tool_capability(self):
        for name in (
            "add_comment", "list_comments", "assign_trace", "unassign_trace",
            "list_my_notifications", "mark_notification_read",
        ):
            assert name in rbac.TOOL_CAPABILITY, name
