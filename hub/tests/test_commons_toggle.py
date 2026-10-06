from __future__ import annotations

import pytest
import pytest_asyncio

from hub.abuse import make_rate_limiter
from hub.config import HubConfig
from hub.server import build_mcp_server

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def enabled_config(config: HubConfig) -> HubConfig:
    return config


@pytest_asyncio.fixture
async def disabled_config(config: HubConfig) -> HubConfig:
    import dataclasses

    return dataclasses.replace(config, commons_enabled=False)


async def _tool_names(cfg: HubConfig, session_factory) -> set[str]:
    mcp = build_mcp_server(cfg, session_factory, make_rate_limiter(cfg))
    tools = await mcp.list_tools()
    return {t.name for t in tools}


async def test_mcp_server_reports_the_unified_product_version(enabled_config, session_factory):
    from commontrace import __version__

    mcp = build_mcp_server(enabled_config, session_factory, make_rate_limiter(enabled_config))
    assert mcp.version == __version__


class TestCommonsEnabledByDefault:
    @pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
    def test_default_config_has_commons_enabled(self):
        cfg = HubConfig(database_url="postgresql+asyncpg://x/y")
        assert cfg.commons_enabled is True

    async def test_the_whole_tool_surface_is_present_by_default(self, enabled_config, session_factory):
        names = await _tool_names(enabled_config, session_factory)
        assert names == {
            "get_traces_batch", "contribute_traces_batch", "delete_traces_batch",
            "search_traces", "contribute_trace", "get_trace", "vote_trace",
            "amend_trace", "list_tags",
            "delete_trace", "request_account_deletion",
            "cancel_account_deletion", "confirm_account_deletion",
            "commons_overlap", "commons_search", "commons_export",
            "submit_kb_entry", "list_my_kb_submissions",
            "account_usage", "fleet_outcomes",
            "holdout_assign", "record_occasion_outcome", "value_delivered",
            "working_set",
            "add_comment", "list_comments", "assign_trace", "unassign_trace",
            "list_my_notifications", "mark_notification_read",
            "search_trace_content",
            "tag_trace_subjects", "find_traces_by_subject", "purge_traces_by_subject",
        }


class TestCommonsDisabled:
    async def test_commons_tools_are_entirely_absent(self, disabled_config, session_factory):
        names = await _tool_names(disabled_config, session_factory)
        assert "share_trace" not in names
        assert "unshare_trace" not in names
        assert "commons_overlap" not in names
        assert "commons_search" not in names
        assert "commons_export" not in names
        assert "submit_kb_entry" not in names
        assert "list_my_kb_submissions" not in names

    async def test_the_org_scoped_tools_are_unaffected(self, disabled_config, session_factory):
        names = await _tool_names(disabled_config, session_factory)
        for tool in (
            "search_traces", "contribute_trace", "get_trace",
            "vote_trace", "amend_trace", "list_tags",
            "delete_trace", "request_account_deletion",
            "cancel_account_deletion", "confirm_account_deletion",
            "fleet_outcomes", "holdout_assign", "record_occasion_outcome",
            "add_comment", "list_comments", "assign_trace", "unassign_trace",
            "list_my_notifications", "mark_notification_read",
            "search_trace_content",
            "tag_trace_subjects", "find_traces_by_subject", "purge_traces_by_subject",
        ):
            assert tool in names, tool

    async def test_account_usage_stays_available(self, disabled_config, session_factory):
        names = await _tool_names(disabled_config, session_factory)
        assert "account_usage" in names

    async def test_calling_a_removed_tool_is_an_unknown_tool_error_not_a_refusal(
        self, disabled_config, session_factory
    ):
        from mcp.server.mcpserver.exceptions import ToolError

        mcp = build_mcp_server(disabled_config, session_factory, make_rate_limiter(disabled_config))
        with pytest.raises(ToolError) as exc_info:
            await mcp.call_tool("commons_overlap", {"failures": []})
        assert "unknown tool" in str(exc_info.value).lower()
        assert "not_found" not in str(exc_info.value).lower()


class TestTheHandMaintainedToolInventoriesMatchReality:
    async def test_smoke_expects_exactly_what_the_server_registers(
        self, enabled_config, session_factory
    ):
        from hub import smoke

        assert await _tool_names(enabled_config, session_factory) == set(smoke.EXPECTED_TOOLS)

    async def test_the_core_only_surface_matches_with_commons_disabled(
        self, disabled_config, session_factory
    ):
        from hub import smoke

        assert await _tool_names(disabled_config, session_factory) == set(smoke.CORE_TOOLS)

    async def test_the_generated_client_template_lists_the_real_surface(
        self, enabled_config, session_factory
    ):
        from commontrace.commands import install_cmd

        assert await _tool_names(enabled_config, session_factory) == set(
            install_cmd._HUB_TOOLS
        )

    async def test_every_registered_tool_has_a_capability(
        self, enabled_config, session_factory
    ):
        from hub import rbac

        for name in await _tool_names(enabled_config, session_factory):
            assert name in rbac.TOOL_CAPABILITY, f"{name} has no capability mapping"
