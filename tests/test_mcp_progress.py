"""Native opt-in MCP progress preserves result schemas and correlated stdio frames."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from types import SimpleNamespace

import pytest

from commontrace import mcp_server

pytest.importorskip("mcp")


def payload(result):
    return json.loads(result.content[0].text)


@pytest.mark.parametrize("meta", [None, {}, {"progress_token": True}, {"progressToken": []}])
def test_progress_is_silent_without_a_valid_requested_token(meta):
    sent = []

    async def report(*args, **kwargs):
        sent.append((args, kwargs))

    ctx = SimpleNamespace(request_context=SimpleNamespace(meta=meta), report_progress=report)
    asyncio.run(mcp_server._progress(ctx, 0, "Storing conversation messages"))
    assert sent == []


@pytest.mark.parametrize("meta", [{"progress_token": 0}, {"progressToken": ""}, {"progressToken": "request-a"}])
def test_numeric_zero_and_string_tokens_are_supported(meta):
    sent = []

    async def report(*args, **kwargs):
        sent.append((args, kwargs))

    ctx = SimpleNamespace(request_context=SimpleNamespace(meta=meta), report_progress=report)
    asyncio.run(mcp_server._progress(ctx, 0, "Storing conversation messages"))
    assert sent == [((0,), {"total": 1, "message": "Storing conversation messages"})]


def test_context_is_hidden_from_public_tool_schemas(tmp_path):
    server = mcp_server.build_server(str(tmp_path))
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == set(mcp_server.LOCAL_TOOLS)
    for tool in tools:
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema")
        assert "ctx" not in schema.get("properties", {})


def test_old_sdk_call_signature_remains_supported(tmp_path, monkeypatch):
    from mcp.server.mcpserver import MCPServer

    original = MCPServer.call_tool

    async def legacy(self, name, arguments):
        return await original(self, name, arguments)

    monkeypatch.setattr(MCPServer, "call_tool", legacy)
    server = mcp_server.build_server(str(tmp_path))
    result = asyncio.run(server.call_tool("conversation_add", {
        "space": "test", "session": "one", "messages": [{"text": "The venue is Oslo."}],
    }, object()))
    assert payload(result)["added"] == 1


def test_context_absent_server_keeps_tools_and_internal_argument_hidden(tmp_path, monkeypatch):
    import mcp.server.mcpserver as sdk

    monkeypatch.delattr(sdk, "Context")
    server = mcp_server.build_server(str(tmp_path))
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == set(mcp_server.LOCAL_TOOLS)
    for tool in tools:
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema")
        assert "ctx" not in schema.get("properties", {})
    result = asyncio.run(server.call_tool("conversation_add", {
        "space": "test", "session": "one", "messages": [{"text": "The venue is Oslo."}],
    }))
    assert payload(result)["added"] == 1


def test_sdk_context_is_forwarded_and_failed_tools_never_report_completion(tmp_path):
    from mcp.server.context import ServerRequestContext
    from mcp.server.mcpserver import Context

    class Session:
        def __init__(self):
            self.sent = []

        async def report_progress(self, progress, total, message):
            self.sent.append((progress, total, message))

    session = Session()
    ctx = Context(request_context=ServerRequestContext(
        session=session, lifespan_context=None, protocol_version="2025-11-25", method="tools/call",
        request_id=1, meta={"progress_token": 0},
    ))
    server = mcp_server.build_server(str(tmp_path))
    success = asyncio.run(server.call_tool("conversation_add", {
        "space": "test", "session": "one", "messages": [{"text": "A private venue is Oslo."}],
    }, context=ctx))
    assert payload(success)["added"] == 1
    assert [p for p, _, _ in session.sent] == [0, 1]
    session.sent.clear()
    failure = asyncio.run(server.call_tool("conversation_summarize", {"space": "missing"}, ctx))
    assert payload(failure)["ok"] is False
    assert [p for p, _, _ in session.sent] == [0]
    assert all("Oslo" not in message and "missing" not in message for _, _, message in session.sent)


def test_optional_notification_failure_does_not_rollback_a_write(tmp_path):
    from mcp.server.context import ServerRequestContext
    from mcp.server.mcpserver import Context

    class Session:
        async def report_progress(self, *args):
            raise OSError("transport temporarily unavailable")

    ctx = Context(request_context=ServerRequestContext(
        session=Session(), lifespan_context=None, protocol_version="2025-11-25", method="tools/call",
        request_id=1, meta={"progress_token": "request-a"},
    ))
    server = mcp_server.build_server(str(tmp_path))
    result = asyncio.run(server.call_tool("conversation_add", {
        "space": "test", "session": "one", "messages": [{"text": "The venue is Oslo."}],
    }, ctx))
    assert payload(result)["ok"] and payload(result)["added"] == 1


def test_worker_ingestion_does_not_block_another_tool_request(tmp_path, monkeypatch):
    from commontrace.conversation import Store

    started, release = threading.Event(), threading.Event()
    original = Store.add

    def held_add(self, *args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Store, "add", held_add)
    server = mcp_server.build_server(str(tmp_path))

    async def drive():
        task = asyncio.create_task(server.call_tool("conversation_add", {
            "space": "test", "session": "one", "messages": [{"text": "The venue is Oslo."}],
        }))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert not task.done()
            other = await asyncio.wait_for(server.call_tool("store_status", {}), timeout=1)
            assert payload(other)["ok"]
        finally:
            release.set()
        assert payload(await task)["added"] == 1

    asyncio.run(drive())


def test_progress_does_not_swallow_request_cancellation():
    async def report(*args, **kwargs):
        raise asyncio.CancelledError

    ctx = SimpleNamespace(request_context=SimpleNamespace(meta={"progress_token": "request-a"}),
                          report_progress=report)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mcp_server._progress(ctx, 0, "Storing conversation messages"))


def test_native_stdio_progress_is_opt_in_correlated_and_frames_remain_readable(tmp_path):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    received = []

    class RecordingRead:
        def __init__(self, stream):
            self.stream = stream

        async def receive(self):
            item = await self.stream.receive()
            message = getattr(item, "message", None)
            message = getattr(message, "root", message)
            if getattr(message, "method", None) == "notifications/progress":
                received.append(dict(message.params))
            return item

        def __getattr__(self, name):
            return getattr(self.stream, name)

        async def __aenter__(self):
            await self.stream.__aenter__()
            return self

        async def __aexit__(self, *args):
            await self.stream.__aexit__(*args)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return await self.receive()
            except anyio.EndOfStream:
                raise StopAsyncIteration from None

    async def drive():
        env = {**os.environ, "COMMONTRACE_MCP_WARM": "0", "COMMONTRACE_CONVERSATION_EMBEDDER": "none"}
        params = StdioServerParameters(command=sys.executable,
                                      args=["-m", "commontrace.cli", "serve", "--dest", str(tmp_path)], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(RecordingRead(read), write) as client:
                await client.initialize()
                arguments = {"space": "private-customer-name", "session": "first",
                             "messages": [{"text": "Private project budget is 900 units."}]}
                default = await client.call_tool("conversation_add", arguments)
                assert payload(default)["added"] == 1 and received == []
                callbacks = [[], []]

                async def first(progress, total, message):
                    callbacks[0].append((progress, total, message))

                async def second(progress, total, message):
                    callbacks[1].append((progress, total, message))

                results = await asyncio.gather(
                    client.call_tool("conversation_add", {**arguments, "session": "second"},
                                     progress_callback=first),
                    client.call_tool("conversation_add", {**arguments, "session": "third"},
                                     progress_callback=second),
                )
                assert all(payload(result)["added"] == 1 for result in results)
                assert all([value for value, _, _ in events] == [0, 1] for events in callbacks)
                assert len(received) == 4
                assert len({event["progressToken"] for event in received}) == 2
                assert all(event["total"] == 1 for event in received)
                encoded = json.dumps(received)
                assert "private-customer-name" not in encoded and "900" not in encoded
                assert "Private project" not in encoded
                assert payload(await client.call_tool("store_status", {}))["ok"]

    # The CLI requires an initialized source root, exactly like the normal MCP client.
    from commontrace.cli import main

    assert main(["init", "--dest", str(tmp_path)]) == 0
    asyncio.run(drive())
