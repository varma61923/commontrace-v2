"""Least-privilege workload tokens: what one key may do, not just whose.

Before scopes there was one identity per org and one privilege level, so a
credential minted for a CI job could also delete the organization. The
audit's exit criterion for this row is narrow and checkable -- "least
privilege keys cannot escalate" -- so these tests are mostly about the
refusals, and specifically about the ways a refusal could be accidentally
undone:

  * a tool registered without a scope decision at all (the old default:
    any authenticated key may call anything),
  * a scope silently implying another,
  * a narrow key reaching a wide tool because enforcement lives in the
    handler rather than at registration,
  * an existing key breaking on upgrade, which is how a security change
    gets reverted in practice.
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

from hub import auth, scopes
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization
from hub.server import build_mcp_server


def payload(result) -> dict:
    """The dict a tool returned, out of whichever envelope the SDK used."""
    if getattr(result, "structured_content", None):
        sc = result.structured_content
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


@pytest_asyncio.fixture
async def org(session_factory) -> str:
    async with session_scope(session_factory) as session:
        organization = Organization(name="scoped-key-org")
        session.add(organization)
        await session.flush()
        return str(organization.id)


@pytest_asyncio.fixture
async def mcp(config, session_factory):
    return build_mcp_server(config, session_factory, make_rate_limiter(config))


class _Scoped:
    """Run a block as a key holding exactly `granted`."""

    def __init__(self, org_id: str, granted):
        self._org_id = org_id
        self._granted = granted

    def __enter__(self):
        self._org = auth.current_org_id.set(self._org_id)
        self._actor = auth.current_actor.set("ct_live_test")
        self._scopes = auth.current_scopes.set(self._granted)
        return self

    def __exit__(self, *exc):
        auth.current_scopes.reset(self._scopes)
        auth.current_actor.reset(self._actor)
        auth.current_org_id.reset(self._org)
        return False


class TestTheScopeVocabulary:
    def test_parse_normalizes_order_and_duplicates(self):
        assert scopes.parse("write,read,write") == ("read", "write")
        assert scopes.parse(["admin", "read"]) == ("read", "admin")

    def test_parse_defaults_to_every_scope(self):
        """The pre-scopes behaviour, preserved: `issue-key <org>` with no
        scope argument must keep minting the key it always did."""
        assert scopes.parse(None) == scopes.ALL_SCOPES

    def test_an_unknown_scope_is_refused_not_dropped(self):
        """A typo that silently narrows a credential is a privilege change
        nobody reviews; one that silently widens it is worse."""
        with pytest.raises(scopes.ScopeError, match="unknown scope"):
            scopes.parse("read,wrote")

    def test_an_empty_scope_list_is_refused(self):
        with pytest.raises(scopes.ScopeError, match="no scopes"):
            scopes.parse("")

    def test_scopes_do_not_imply_each_other(self):
        """The property that makes "can this key escalate?" answerable by
        reading one row instead of simulating a hierarchy."""
        assert not scopes.satisfies(("admin",), scopes.SCOPE_READ)
        assert not scopes.satisfies(("write",), scopes.SCOPE_READ)
        assert not scopes.satisfies(("read",), scopes.SCOPE_WRITE)

    def test_a_legacy_key_with_no_scope_list_holds_everything(self):
        """NULL means "issued before the column existed", which is a key
        that could do all of this. An EMPTY list is a deliberate narrowing
        and must not be confused with it."""
        for scope in scopes.ALL_SCOPES:
            assert scopes.satisfies(None, scope)
            assert not scopes.satisfies((), scope)


@pytest.mark.asyncio
class TestEveryToolDeclaresAScope:
    async def test_no_tool_is_registered_without_a_scope_decision(self, mcp):
        """The failure this prevents: a tool added later that silently
        inherits "any authenticated key may call this", which is the exact
        state the whole surface was in before scopes existed. Registration
        goes through `scoped_tool`, so a missing decision cannot compile --
        this asserts nobody routed around it with a bare `@mcp.tool()`."""
        registered = {tool.name for tool in await mcp.list_tools()}
        declared = set(mcp.commontrace_tool_scopes)
        assert registered == declared, (
            "every registered tool must declare a scope; missing: "
            f"{sorted(registered - declared)}"
        )

    async def test_every_declared_scope_is_a_real_scope(self, mcp):
        assert set(mcp.commontrace_tool_scopes.values()) <= set(scopes.ALL_SCOPES)

    async def test_the_destructive_tools_are_admin_scoped(self, mcp):
        """Named explicitly rather than derived, so that moving one of these
        to a weaker scope has to be a deliberate edit to this list."""
        declared = mcp.commontrace_tool_scopes
        for tool in (
            "delete_trace",
            "request_account_deletion",
            "cancel_account_deletion",
            "confirm_account_deletion",
        ):
            assert declared[tool] == scopes.SCOPE_ADMIN, tool

    async def test_the_corpus_mutating_tools_are_write_scoped(self, mcp):
        declared = mcp.commontrace_tool_scopes
        for tool in (
            "contribute_trace", "amend_trace", "vote_trace",
            "holdout_assign", "record_occasion_outcome",
        ):
            assert declared[tool] == scopes.SCOPE_WRITE, tool


@pytest.mark.asyncio
class TestANarrowKeyCannotEscalate:
    async def test_a_read_only_key_cannot_contribute(self, mcp, org):
        with _Scoped(org, ("read",)):
            result = payload(await mcp.call_tool("contribute_trace", {
                "title": "t", "context_text": "c", "solution_text": "s",
                "agent_type": "code",
            }))
        assert result["error"] == "forbidden"
        assert result["required_scope"] == scopes.SCOPE_WRITE
        assert result["granted_scopes"] == ["read"]

    async def test_a_read_only_key_cannot_delete_a_trace(self, mcp, org):
        with _Scoped(org, ("read",)):
            result = payload(await mcp.call_tool("delete_trace", {"id": "x"}))
        assert result["error"] == "forbidden"
        assert result["required_scope"] == scopes.SCOPE_ADMIN

    async def test_a_read_write_key_still_cannot_delete_the_organization(self, mcp, org):
        """The separation that matters most commercially: a production agent
        holds read+write forever, and must not be one compromised prompt away
        from ending the account."""
        with _Scoped(org, ("read", "write")):
            result = payload(await mcp.call_tool("request_account_deletion", {}))
        assert result["error"] == "forbidden"
        assert result["required_scope"] == scopes.SCOPE_ADMIN

    async def test_an_admin_only_key_cannot_read_the_corpus(self, mcp, org):
        """Scopes do not imply each other, at the tool surface and not only
        in the helper."""
        with _Scoped(org, ("admin",)):
            result = payload(await mcp.call_tool("search_traces", {"query": "x"}))
        assert result["error"] == "forbidden"
        assert result["required_scope"] == scopes.SCOPE_READ

    async def test_a_key_with_no_scopes_can_do_nothing(self, mcp, org):
        with _Scoped(org, ()):
            for tool, args in (
                ("search_traces", {"query": "x"}),
                ("list_tags", {}),
                ("account_usage", {}),
            ):
                assert payload(await mcp.call_tool(tool, args))["error"] == "forbidden"

    async def test_the_refusal_says_re_authenticating_will_not_help(self, mcp, org):
        """A caller that reads this as a transient auth failure will retry
        forever with the same key."""
        with _Scoped(org, ("read",)):
            result = payload(await mcp.call_tool("vote_trace", {"id": "x", "vote": "up"}))
        assert "will not help" in result["detail"]


@pytest.mark.asyncio
class TestAWideKeyStillWorks:
    async def test_a_full_scope_key_reaches_a_read_tool(self, mcp, org):
        with _Scoped(org, scopes.ALL_SCOPES):
            result = payload(await mcp.call_tool("list_tags", {}))
        assert result.get("error") != "forbidden"

    async def test_a_legacy_key_reaches_every_tool_surface(self, mcp, org):
        """The upgrade property. A key issued before this column existed
        carries no scope list, and must keep working exactly as it did --
        otherwise the security change is the thing that takes a customer's
        fleet down, and it gets reverted."""
        with _Scoped(org, None):
            for tool, args in (
                ("search_traces", {"query": "x"}),
                ("list_tags", {}),
                ("account_usage", {}),
            ):
                assert payload(await mcp.call_tool(tool, args)).get("error") != "forbidden"


@pytest.mark.asyncio
class TestIssuanceRecordsTheGrant:
    async def test_a_key_is_issued_with_every_scope_by_default(self, session_factory, org):
        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, org)
        assert issued.scopes == scopes.ALL_SCOPES

    async def test_a_narrow_key_is_stored_and_verified_narrow(self, session_factory, org):
        """End to end through the real verification path, not just the
        issuance return value: the scopes a request is judged against are
        the ones read back out of the row."""
        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, org, scopes="read")
        assert issued.scopes == ("read",)

        async with session_scope(session_factory) as session:
            verified = await auth.verify_api_key(session, issued.raw_key)
        assert verified is not None
        assert verified.scopes == ("read",)
        assert not scopes.satisfies(verified.scopes, scopes.SCOPE_ADMIN)

    async def test_an_unknown_scope_issues_no_key_at_all(self, session_factory, org):
        """Validated before the row exists, so a rejected request cannot
        leave a half-issued credential behind."""
        from sqlalchemy import func, select

        from hub.models import ApiKey

        async with session_scope(session_factory) as session:
            before = (await session.execute(
                select(func.count(ApiKey.id)).where(ApiKey.org_id == org)
            )).scalar_one()

        with pytest.raises(scopes.ScopeError):
            async with session_scope(session_factory) as session:
                await auth.issue_api_key(session, org, scopes="read,superuser")

        async with session_scope(session_factory) as session:
            after = (await session.execute(
                select(func.count(ApiKey.id)).where(ApiKey.org_id == org)
            )).scalar_one()
        assert after == before


@pytest.mark.asyncio
class TestRequireScopeOutsideARequest:
    async def test_operator_paths_are_not_gated_by_scopes(self):
        """hub/manage.py, the benchmarks and alembic run with no
        authenticated key in context. They were never gated by a key's
        capabilities, and gating them now would break the operator CLI in
        the name of restricting credentials it does not use."""
        auth.require_scope(scopes.SCOPE_ADMIN)  # must not raise
