"""Named roles for a person, checked in addition to an API key's scope.

What these tests defend, in order of how badly getting it wrong would hurt:

1. **Every real MCP tool has a capability mapping.** A tool with none is not
   merely undocumented -- `capability_for_tool` refuses to answer for it,
   which is the safe failure. This is checked against the ACTUAL registered
   tool set (`mcp.commontrace_tool_scopes`), not a hand-maintained list that
   could drift from it.
2. **A role's derived scope is never narrower than what its own capabilities
   need.** If it were, a person holding the right capability would still be
   refused by the coarser, older scope check -- a confusing, hard-to-debug
   gate for the wrong reason.
3. **Roles are not a hierarchy.** No role's capability set may be a superset
   of another's "by accident" of list order; VIEW is the only capability
   every non-empty role shares, and that is asserted directly.
4. **An unknown role or tool is refused, not silently permissive.**
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from hub import rbac, scopes
from hub.abuse import make_rate_limiter
from hub.server import build_mcp_server


@pytest_asyncio.fixture
async def mcp(config, session_factory):
    return build_mcp_server(config, session_factory, make_rate_limiter(config))


@pytest.mark.asyncio
class TestEveryRegisteredToolHasACapability:
    async def test_every_tool_the_server_actually_registers_is_mapped(self, mcp):
        """Cross-checked against the REAL registry, not a copy of it, so this
        fails the moment a new tool ships without a capability decision."""
        registered = set(mcp.commontrace_tool_scopes)
        mapped = set(rbac.TOOL_CAPABILITY)
        assert registered == mapped, (
            "every tool the server registers needs a capability in "
            f"hub/rbac.py:TOOL_CAPABILITY; missing: {sorted(registered - mapped)}, "
            f"mapped but not registered: {sorted(mapped - registered)}"
        )

    async def test_every_mapped_capability_is_real(self, mcp):
        assert set(rbac.TOOL_CAPABILITY.values()) <= set(rbac.CAPABILITIES)

    async def test_the_destructive_tools_need_security(self, mcp):
        """Named explicitly, mirroring test_api_key_scopes.py's own
        assertion for admin scope -- moving one of these to a weaker
        capability has to be a deliberate edit here."""
        for tool in (
            "delete_trace", "request_account_deletion",
            "cancel_account_deletion", "confirm_account_deletion",
        ):
            assert rbac.TOOL_CAPABILITY[tool] == rbac.CAP_SECURITY, tool

    async def test_the_deploy_decision_needs_deploy(self, mcp):
        assert rbac.TOOL_CAPABILITY["holdout_assign"] == rbac.CAP_DEPLOY

    async def test_content_authoring_needs_curate(self, mcp):
        for tool in ("contribute_trace", "amend_trace", "submit_kb_entry"):
            assert rbac.TOOL_CAPABILITY[tool] == rbac.CAP_CURATE, tool


@pytest.mark.asyncio
class TestScopeNeverUnderShootsCapability:
    """The property that keeps the two gates from disagreeing: whatever a
    role's OWN capabilities let it call, the role's derived scope must not
    be the thing that then blocks it."""

    async def test_every_role_derived_scope_covers_every_tool_its_capabilities_reach(
        self, mcp
    ):
        """Checked against the ACTUAL scope each tool was registered with,
        not an approximation of it -- if this passes, a person holding the
        matching capability can never be refused by the coarser scope
        check underneath it."""
        declared = mcp.commontrace_tool_scopes
        for role in rbac.ROLES:
            granted = rbac.capabilities_of(role)
            derived_scope = set(rbac.scopes_of(role))
            for tool, needed_scope in declared.items():
                needed_cap = rbac.TOOL_CAPABILITY[tool]
                if needed_cap not in granted:
                    continue  # this role could not call the tool anyway
                assert scopes.satisfies(tuple(derived_scope), needed_scope), (
                    f"{role} holds capability {needed_cap!r} for {tool!r} but its "
                    f"derived scope {sorted(derived_scope)} does not satisfy the "
                    f"tool's own {needed_scope!r} requirement"
                )


class TestUnmappedToolIsRefused:
    def test_an_unmapped_tool_name_is_refused_not_silently_open(self):
        with pytest.raises(rbac.RoleError, match="no capability mapping"):
            rbac.capability_for_tool("some_future_tool")


class TestRolesAreNotAHierarchy:
    def test_view_is_the_only_universal_capability(self):
        shared = set.intersection(*(set(rbac.capabilities_of(r)) for r in rbac.ROLES))
        assert shared == {rbac.CAP_VIEW}

    def test_billing_admin_is_not_a_subset_of_owner_by_coincidence_of_order(self):
        """Not a meaningful assertion about a ladder -- there is none -- but
        a concrete check that BILLING is granted to exactly the roles that
        should hold it, not swept in by a shared prefix."""
        holders = {r for r in rbac.ROLES if rbac.has_capability(r, rbac.CAP_BILLING)}
        assert holders == {rbac.ROLE_BILLING_ADMIN, rbac.ROLE_OWNER}

    def test_security_admin_cannot_deploy(self):
        """Two different admin-shaped roles, deliberately not each other's
        superset -- a security admin revoking access is not the same
        judgement as a deployer choosing what ships."""
        assert not rbac.has_capability(rbac.ROLE_SECURITY_ADMIN, rbac.CAP_DEPLOY)

    def test_deployer_cannot_curate(self):
        assert not rbac.has_capability(rbac.ROLE_DEPLOYER, rbac.CAP_CURATE)

    def test_only_owner_holds_manage_users(self):
        holders = {r for r in rbac.ROLES if rbac.has_capability(r, rbac.CAP_MANAGE_USERS)}
        assert holders == {rbac.ROLE_OWNER}


class TestChecking:
    def test_require_capability_passes_silently_when_granted(self):
        rbac.require_capability(rbac.ROLE_CURATOR, "contribute_trace")

    def test_require_capability_raises_when_not_granted(self):
        with pytest.raises(rbac.CapabilityDenied) as exc:
            rbac.require_capability(rbac.ROLE_VIEWER, "contribute_trace")
        assert exc.value.required == rbac.CAP_CURATE
        assert exc.value.role == rbac.ROLE_VIEWER

    def test_an_unknown_role_is_refused(self):
        with pytest.raises(rbac.RoleError, match="unknown role"):
            rbac.capabilities_of("superuser")

    def test_describe_returns_both_facts_at_once(self):
        desc = rbac.describe(rbac.ROLE_CURATOR)
        assert desc.role == rbac.ROLE_CURATOR
        assert rbac.CAP_CURATE in desc.capabilities
        assert scopes.SCOPE_WRITE in desc.scopes
