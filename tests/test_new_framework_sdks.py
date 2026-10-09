"""Pinned real SDK factories and execution, without providers or model calls."""
from __future__ import annotations

import asyncio
import json

import pytest

from commontrace.frameworks import MemoryTools


def test_google_adk_executes_owner_bound_tools(tmp_path):
    pytest.importorskip("google.adk.tools")
    tools = MemoryTools(str(tmp_path), "adk", "session").tools("google-adk")
    async def run():
        await tools[1].run_async(args={"user": "Office is Tokyo", "assistant": "Acknowledged"}, tool_context=None)
        result = await tools[0].run_async(args={"query": "office"}, tool_context=None)
        assert "Tokyo" in result
    asyncio.run(run())


def test_strands_executes_owner_bound_tools(tmp_path):
    pytest.importorskip("strands")
    tools = MemoryTools(str(tmp_path), "strands", "session").tools("strands")
    tools[1](user="Office is Tokyo", assistant="Acknowledged")
    assert "Tokyo" in tools[0](query="office")


def test_ag2_public_registration_and_execution(tmp_path):
    sdk = pytest.importorskip("ag2")
    from ag2.context import ConversationContext
    from ag2.events import ToolCallEvent

    memory = MemoryTools(str(tmp_path), "ag2", "session")
    memory.remember("Office is Tokyo", "Acknowledged")
    tools = memory.tools("ag2")
    agent = sdk.Agent("test", tools=tools)
    assert agent is not None and tools[0].name == "commontrace_recall"
    # No send occurs during an ordinary FunctionTool invocation; a stream is
    # only needed for tool-side messaging, which these memory tools never use.
    context = ConversationContext(stream=None)
    event = ToolCallEvent(name="commontrace_recall", arguments=json.dumps({"query": "office"}))
    reply = asyncio.run(tools[0](event, context))
    assert "Tokyo" in str(reply.result)
