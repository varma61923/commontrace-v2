"""Native MCP tools expose the same durable learning protocol as HTTP."""

import asyncio
import json

import pytest

from unimem import mcp_server
from unimem.learning_contract import OPERATIONS, SCHEMA
from unimem.memory_journal import Journal
from unimem.memory_record import MemoryRecord

pytest.importorskip("mcp")


def call(server, tool, **arguments):
    result = asyncio.run(server.call_tool(tool, arguments))
    value = getattr(result, "structured_content", None)
    if value:
        return value.get("result", value)
    return json.loads(result.content[0].text)


def test_tools_and_real_roundtrip(tmp_path):
    root = str(tmp_path)
    Journal(root).put(MemoryRecord("source", "fact", 1, {"text": "Inspect constraints."}))
    server = mcp_server.build_server(root)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {"learning_" + operation for operation in OPERATIONS} <= names
    assert call(server, "learning_capabilities")["schema"] == SCHEMA
    assert call(server, "learning_configure", mode="observe")["config"]["mode"] == "observe"
    assignment = call(server, "learning_assign", occasion_id="task", memory_ids=["source"])
    assert assignment["schema"] == SCHEMA
    assert (
        call(server, "learning_delivery", occasion_id="task", memory_ids=assignment["inject"])["delivery"]
        == assignment["inject"]
    )
    assert call(server, "learning_outcome", occasion_id="task", succeeded=True)["outcome"]["succeeded"]
    assert call(server, "learning_status")["occasions"] == 1
    assert call(server, "learning_run")["applied"] is False
    assert call(server, "learning_rollback", reason="review")["restored_revision"] == 0


def test_restricted_toolset_excludes_policy_administration(tmp_path):
    server = mcp_server.build_server(str(tmp_path), allow_approval=False)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {"learning_assign", "learning_delivery", "learning_outcome", "learning_status"} <= names
    assert not {"learning_configure", "learning_run", "learning_rollback"} & names
