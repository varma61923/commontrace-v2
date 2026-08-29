"""HUB_COMMONS_ENABLED=false: the cross-org commons removed, not just refused.

Prompted by a real deployment shape: an internal-only B2B offering that must
have "no common knowledge" with any other organization until traction is
proven. `share_trace`/`unshare_trace`/`commons_overlap` being opt-in and
unused by every org already gets there in practice -- but "in practice" is
discipline, not a guarantee, and this deployment's whole point is not
depending on discipline. This tests the guarantee: with the flag off, the
three commons tools are absent from the MCP tool surface entirely, so a
client that tries gets the framework's own "unknown tool" error rather than
a per-call refusal that some future call site could forget to apply.

account_usage is checked as the control: it reports an org's own plan and
its own usage, never another org's data, so disabling the commons must not
disable it.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from hub.abuse import make_rate_limiter
from hub.config import HubConfig
from hub.server import build_mcp_server

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def enabled_config(config: HubConfig) -> HubConfig:
    return config  # the `config` fixture already defaults commons_enabled=True


@pytest_asyncio.fixture
async def disabled_config(config: HubConfig) -> HubConfig:
    import dataclasses

    return dataclasses.replace(config, commons_enabled=False)


async def _tool_names(cfg: HubConfig, session_factory) -> set[str]:
    mcp = build_mcp_server(cfg, session_factory, make_rate_limiter(cfg))
    tools = await mcp.list_tools()
    return {t.name for t in tools}


async def test_mcp_server_reports_the_unified_product_version(enabled_config, session_factory):
    """PROTOCOL.md #9 explicitly unified the package/protocol/CLI version
    numbers into one 2.0.0 the whole product reports identically, to stop
    drift like a stale hardcoded MCP server version -- this Hub used to
    announce "0.1.0" to every connecting client regardless of the actual
    2.0.0 product/protocol version everywhere else."""
    from commontrace import __version__

    mcp = build_mcp_server(enabled_config, session_factory, make_rate_limiter(enabled_config))
    assert mcp.version == __version__


class TestCommonsEnabledByDefault:
    """Existing deployments that never set HUB_COMMONS_ENABLED must see no
    change -- the default has to preserve current behavior, not opt every
    install into a narrower one."""

    @pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
    def test_default_config_has_commons_enabled(self):
        cfg = HubConfig(database_url="postgresql+asyncpg://x/y")
        assert cfg.commons_enabled is True

    async def test_all_eleven_tools_present_by_default(self, enabled_config, session_factory):
        names = await _tool_names(enabled_config, session_factory)
        assert names == {
            "search_traces", "contribute_trace", "get_trace", "vote_trace",
            "amend_trace", "list_tags", "share_trace", "unshare_trace",
            "commons_overlap", "commons_search", "account_usage",
        }


class TestCommonsDisabled:
    async def test_commons_tools_are_entirely_absent(self, disabled_config, session_factory):
        names = await _tool_names(disabled_config, session_factory)
        assert "share_trace" not in names
        assert "unshare_trace" not in names
        assert "commons_overlap" not in names
        # commons_search reads other orgs' shared traces exactly as
        # commons_overlap does, so a deployment that turned cross-org
        # sharing off must not acquire a second door to it.
        assert "commons_search" not in names

    async def test_the_six_org_scoped_tools_are_unaffected(self, disabled_config, session_factory):
        names = await _tool_names(disabled_config, session_factory)
        for tool in (
            "search_traces", "contribute_trace", "get_trace",
            "vote_trace", "amend_trace", "list_tags",
        ):
            assert tool in names, tool

    async def test_account_usage_stays_available(self, disabled_config, session_factory):
        """It reports the caller's OWN plan and usage -- never another
        org's data -- so disabling cross-org sharing must not remove it."""
        names = await _tool_names(disabled_config, session_factory)
        assert "account_usage" in names

    async def test_calling_a_removed_tool_is_an_unknown_tool_error_not_a_refusal(
        self, disabled_config, session_factory
    ):
        """The distinction that makes this a real guarantee: the tool does
        not exist to be called, rather than existing and saying no. A
        refusal is one code path that could regress; absence cannot."""
        from mcp.server.mcpserver.exceptions import ToolError

        mcp = build_mcp_server(disabled_config, session_factory, make_rate_limiter(disabled_config))
        with pytest.raises(ToolError) as exc_info:
            await mcp.call_tool("commons_overlap", {"failures": []})
        # "Unknown tool", not this project's own not_found/tenant-isolation
        # refusal shape -- the tool never existed to run and decide,
        # rather than existing and choosing to say no.
        assert "unknown tool" in str(exc_info.value).lower()
        assert "not_found" not in str(exc_info.value).lower()
