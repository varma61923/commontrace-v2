"""Real SDK clients verify the authenticated local MCP TCP transports."""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")
uvicorn = pytest.importorskip("uvicorn")

from mcp import ClientSession  # noqa: E402
from mcp.client.sse import sse_client  # noqa: E402

from commontrace import gateway_tokens, mcp_server  # noqa: E402
from commontrace.cli import main  # noqa: E402
from commontrace.conversation.store import Store, db_path  # noqa: E402
from commontrace.mcp_transport import build_http_app  # noqa: E402


@contextlib.contextmanager
def running_server(root: Path, transport: str, **kwargs: Any) -> Iterator[str]:
    """Bind once to an ephemeral TCP port, and always settle server workers."""
    app = build_http_app(str(root), transport=transport, **kwargs)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        deadline = time.monotonic() + 10
        try:
            while not server.started:
                assert worker.is_alive(), "MCP HTTP server exited during startup"
                assert time.monotonic() < deadline, "MCP HTTP server did not start"
                time.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            worker.join(timeout=10)
            assert not worker.is_alive(), "MCP HTTP server did not settle"


@contextlib.asynccontextmanager
async def connected(
    base: str, transport: str, token: str,
    sessions: list[Callable[[], str | None]] | None = None,
) -> AsyncIterator[ClientSession]:
    headers = {"Authorization": f"Bearer {token}"}
    if transport == "sse":
        async with sse_client(f"{base}/sse", headers=headers) as streams:
            async with ClientSession(streams[0], streams[1]) as client:
                yield client
        return
    sdk = importlib.import_module("mcp.client.streamable_http")
    connect = getattr(sdk, "streamable_http_client", None) or sdk.streamablehttp_client
    if "http_client" in inspect.signature(connect).parameters:
        httpx = getattr(sdk, "httpx2", None) or getattr(sdk, "httpx", None)
        if httpx is None:
            httpx = importlib.import_module("httpx")

        async def remember_session(response: Any) -> None:
            session_id = response.headers.get("mcp-session-id")
            if sessions is not None and session_id:
                sessions.append(lambda: session_id)

        async with httpx.AsyncClient(headers=headers, event_hooks={"response": [remember_session]}) as http:
            async with connect(f"{base}/mcp", http_client=http) as streams:
                if sessions is not None and len(streams) > 2:
                    sessions.append(streams[2])
                async with ClientSession(streams[0], streams[1]) as client:
                    yield client
    else:
        async with connect(f"{base}/mcp", headers=headers) as streams:
            if sessions is not None:
                sessions.append(streams[2])
            async with ClientSession(streams[0], streams[1]) as client:
                yield client


def request_status(
    url: str, *, token: str = "", headers: dict[str, str] | None = None, body: bytes | None = None,
) -> int:
    supplied = dict(headers or {})
    if token:
        supplied["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=supplied, data=body)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
def test_sdk_negotiation_tools_resources_and_correlated_progress(tmp_path: Path, transport: str) -> None:
    token = gateway_tokens.load_or_create_token(str(tmp_path))
    with running_server(tmp_path, transport) as base:
        async def exercise() -> None:
            async with connected(base, transport, token) as client:
                initialized = await client.initialize()
                info = getattr(initialized, "server_info", None) or initialized.serverInfo
                assert info.name == "commontrace-local"
                tools = await client.list_tools()
                assert {tool.name for tool in tools.tools} == set(mcp_server.LOCAL_TOOLS)
                assert all("ctx" not in (
                    getattr(tool, "input_schema", None) or tool.inputSchema
                ).get("properties", {}) for tool in tools.tools)
                resources = await client.list_resources()
                assert "commontrace://profile" in {str(resource.uri) for resource in resources.resources}
                profile = await client.read_resource("commontrace://profile")
                assert profile.contents
                progress: list[float] = []

                async def on_progress(value: float, total: float | None, message: str | None) -> None:
                    progress.append(value)
                    assert total == 1
                    assert message is None or "private-venue" not in message

                result = await client.call_tool("conversation_add", {
                    "space": "private-venue", "session": "network-session",
                    "messages": [{"text": "The venue is Oslo."}],
                }, progress_callback=on_progress)
                assert json.loads(result.content[0].text)["added"] == 1
                assert progress == [0, 1]
                status = await client.call_tool("store_status", {})
                assert json.loads(status.content[0].text)["ok"] is True

        asyncio.run(exercise())


@pytest.mark.parametrize("transport,path", [("sse", "/sse"), ("streamable-http", "/mcp")])
def test_network_auth_rotation_and_dns_boundary(tmp_path: Path, transport: str, path: str) -> None:
    token = gateway_tokens.load_or_create_token(str(tmp_path))
    with running_server(tmp_path, transport) as base:
        assert request_status(base + path) == 401
        assert request_status(base + path + f"?token={token}") == 401
        assert request_status(base + path, token=token, headers={"Host": "attacker.example"}) == 421
        assert request_status(base + path, token=token, headers={"Origin": "https://attacker.example"}) == 403
        replacement = gateway_tokens.rotate_token(str(tmp_path))
        assert request_status(base + path, token=token) == 401
        # A valid bearer reaches SDK framing (missing Accept/session), whereas
        # revoked credentials are refused before the SDK reads a request.
        if transport == "streamable-http":
            assert request_status(base + path, token=replacement) != 401
        gateway_tokens.revoke_token(str(tmp_path))
        assert request_status(base + path, token=replacement) == 401


def test_active_sse_stream_has_bounded_admission(tmp_path: Path) -> None:
    token = gateway_tokens.load_or_create_token(str(tmp_path))
    with running_server(tmp_path, "sse", max_connections=1) as base:
        async def exercise() -> None:
            async with sse_client(base + "/sse", headers={"Authorization": f"Bearer {token}"}):
                assert await asyncio.to_thread(request_status, base + "/sse", token=token) == 503

        asyncio.run(exercise())


def test_streamable_session_id_cannot_bypass_revoked_authorization(tmp_path: Path) -> None:
    token = gateway_tokens.load_or_create_token(str(tmp_path))
    with running_server(tmp_path, "streamable-http") as base:
        async def exercise() -> None:
            sessions: list[Callable[[], str | None]] = []
            async with connected(base, "streamable-http", token, sessions) as client:
                await client.initialize()
                session_id = sessions[0]()
                assert session_id
                bad_requests = await asyncio.gather(*[
                    asyncio.to_thread(request_status, base + "/mcp", headers={
                        "Mcp-Session-Id": session_id, "Accept": "text/event-stream",
                    }) for _ in range(12)
                ])
                assert bad_requests == [401] * 12
                assert (await client.list_tools()).tools
                gateway_tokens.revoke_token(str(tmp_path))
                assert await asyncio.to_thread(request_status, base + "/mcp", token=token, headers={
                    "Mcp-Session-Id": session_id, "Accept": "text/event-stream",
                }) == 401

        asyncio.run(exercise())


@pytest.mark.parametrize("transport,path", [("sse", "/sse"), ("streamable-http", "/mcp")])
def test_cli_network_transports_preserve_no_approval_policy(
    tmp_path: Path, transport: str, path: str,
) -> None:
    assert main(["init", "--dest", str(tmp_path)]) == 0
    token = gateway_tokens.load_or_create_token(str(tmp_path))
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "COMMONTRACE_MCP_WARM": "0", "COMMONTRACE_CONVERSATION_EMBEDDER": "none"}
    process = subprocess.Popen([
        sys.executable, "-m", "commontrace.cli", "serve", "--dest", str(tmp_path),
        "--transport", transport, "--port", str(port), "--no-approval",
    ], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while True:
            assert process.poll() is None, "MCP CLI exited during startup"
            try:
                if request_status(base + path) == 401:
                    break
            except urllib.error.URLError:
                pass
            assert time.monotonic() < deadline, "MCP CLI did not open its listener"
            time.sleep(0.02)

        async def exercise() -> None:
            async with connected(base, transport, token) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert {tool.name for tool in tools.tools} == (
                    set(mcp_server.LOCAL_TOOLS) - set(mcp_server.APPROVAL_TOOLS)
                )
                status = await client.call_tool("store_status", {})
                assert json.loads(status.content[0].text)["ok"] is True

        asyncio.run(exercise())
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        assert token.encode("utf-8") not in stdout + stderr


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
def test_cancelled_network_call_leaves_session_usable(tmp_path: Path, transport: str) -> None:
    """A real database lock holds the write while cancellation crosses the wire."""
    token = gateway_tokens.load_or_create_token(str(tmp_path))
    with Store(str(tmp_path), "locked-space"):
        pass
    with running_server(tmp_path, transport) as base:
        async def exercise() -> None:
            async with connected(base, transport, token) as client:
                await client.initialize()
                started = asyncio.Event()

                async def on_progress(value: float, total: float | None, message: str | None) -> None:
                    if value == 0:
                        started.set()

                lock = sqlite3.connect(db_path(str(tmp_path), "locked-space"))
                lock.execute("BEGIN IMMEDIATE")
                pending = asyncio.create_task(client.call_tool("conversation_add", {
                    "space": "locked-space", "session": "cancelled-call",
                    "messages": [{"text": "Retain the accepted write after caller cancellation."}],
                }, progress_callback=on_progress))
                try:
                    await asyncio.wait_for(started.wait(), timeout=3)
                    assert not pending.done()
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                finally:
                    lock.rollback()
                    lock.close()
                status = await asyncio.wait_for(client.call_tool("store_status", {}), timeout=3)
                assert json.loads(status.content[0].text)["ok"] is True

        asyncio.run(exercise())


def test_authenticated_streamable_body_limit_is_enforced(tmp_path: Path) -> None:
    token = gateway_tokens.load_or_create_token(str(tmp_path))
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "x" * 2048, "version": "1"},
        },
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    with running_server(tmp_path, "streamable-http", max_request_body_size=1024) as base:
        assert request_status(base + "/mcp", headers=headers, body=body) == 401
        assert request_status(base + "/mcp", token=token, headers=headers, body=body) == 413
