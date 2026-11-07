import pytest

from unimem import hub_client


class TestRetryPolicy:
    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("read timeout"),
            ConnectionRefusedError("connection refused"),
            ConnectionResetError("connection reset by peer"),
            RuntimeError("server returned 503 Service Unavailable"),
            RuntimeError("upstream returned 502"),
        ],
    )
    def test_transport_failures_are_retried(self, exc):
        assert hub_client._is_retryable(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("401 Unauthorized"),
            RuntimeError("invalid, revoked, or expired API key"),
            RuntimeError("403 Forbidden"),
        ],
    )
    def test_auth_failures_are_not_retried(self, exc):
        assert hub_client._is_retryable(exc) is False

    def test_unknown_failures_are_not_retried(self):
        assert hub_client._is_retryable(ValueError("something we don't recognize")) is False


class TestDefaults:
    def test_a_finite_timeout_is_configured(self):
        assert hub_client.DEFAULT_TIMEOUT_SECONDS > 0
        assert hub_client.DEFAULT_TIMEOUT_SECONDS < 300

    def test_retries_are_bounded(self):
        assert 1 <= hub_client.DEFAULT_MAX_ATTEMPTS <= 5

    def test_backoff_is_positive(self):
        assert hub_client.RETRY_BASE_DELAY_SECONDS > 0


class _Result:
    is_error = False
    content: list = []

    def __init__(self, payload):
        self.structured_content = payload


def _scripted_hub(monkeypatch, failures, payload):
    """Patch the Hub transport: raise each of *failures* in turn, then answer *payload*."""
    import asyncio
    import contextlib

    calls: list[dict] = []

    class Session:
        async def call_tool(self, name, arguments):
            calls.append(dict(arguments))
            if len(calls) <= len(failures):
                raise failures[len(calls) - 1]
            return _Result(payload)

    @contextlib.asynccontextmanager
    async def fake_open(hub_url, api_key, timeout_seconds=30, max_attempts=3, on_first_pause=None):
        session = hub_client.HubSession(hub_url, api_key, timeout_seconds, max_attempts)
        session._session = Session()
        yield session

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(hub_client, "open_hub_session", fake_open)
    monkeypatch.setattr(hub_client.asyncio, "sleep", no_sleep)
    return calls, asyncio.run


class TestWriteReplaySafety:
    def test_kb_submission_retries_carry_one_stable_idempotency_key(self, monkeypatch):
        # Regression: a read timeout after a committed insert was replayed
        # without a key, creating a duplicate KB submission.
        calls, run = _scripted_hub(monkeypatch, [TimeoutError("read timeout")], {"id": "kb-1"})
        result = run(hub_client.submit_kb_entry("https://hub.example", "k", "t", "c", "s"))
        assert result["id"] == "kb-1"
        assert len(calls) == 2
        keys = {call["idempotency_key"] for call in calls}
        assert len(keys) == 1 and next(iter(keys)).startswith("kb:")

    def test_kb_submission_keeps_a_caller_supplied_key(self, monkeypatch):
        calls, run = _scripted_hub(monkeypatch, [], {"id": "kb-1"})
        run(hub_client.submit_kb_entry("https://hub.example", "k", "t", "c", "s", idempotency_key="mine"))
        assert calls == [{**calls[0], "idempotency_key": "mine"}]

    def test_delete_is_not_replayed_after_an_ambiguous_timeout(self, monkeypatch):
        # Regression: the replay of an applied delete answered not_found and the
        # CLI reported "no such trace" for a trace it had just deleted.
        calls, run = _scripted_hub(monkeypatch, [TimeoutError("read timeout")], {"error": "not_found"})
        with pytest.raises(hub_client.HubConnectionError):
            run(hub_client.delete_trace("https://hub.example", "k", "tr-1"))
        assert len(calls) == 1

    def test_delete_is_replayed_when_the_request_never_left(self, monkeypatch):
        calls, run = _scripted_hub(monkeypatch, [ConnectionRefusedError("refused")], {"deleted": True})
        assert run(hub_client.delete_trace("https://hub.example", "k", "tr-1")) is True
        assert len(calls) == 2

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (ConnectionRefusedError("refused"), True),
            (__import__("socket").gaierror("dns"), True),
            (type("ConnectTimeout", (Exception,), {})("connect"), True),
            (TimeoutError("read timeout"), False),
            (ConnectionResetError("reset"), False),
        ],
    )
    def test_never_sent_classification(self, exc, expected):
        wrapped = RuntimeError("transport failed")
        wrapped.__cause__ = exc
        assert hub_client._never_sent(wrapped) is expected


@pytest.mark.parametrize("max_attempts", [0, -1, 1.5, True])
def test_session_rejects_invalid_retry_budget(max_attempts):
    with pytest.raises(hub_client.HubConfigurationError, match="max_attempts"):
        hub_client.HubSession("https://hub.example", "key", 30, max_attempts)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_session_rejects_invalid_rpc_deadline(timeout):
    with pytest.raises(hub_client.HubConfigurationError, match="timeout"):
        hub_client.HubSession("https://hub.example", "key", timeout, 1)


def test_rpc_deadline_cancels_an_unanswered_call_without_replaying_a_write():
    import asyncio

    async def exercise():
        cancelled = asyncio.Event()
        calls = []

        class PendingSession:
            async def call_tool(self, name, arguments):
                calls.append((name, arguments))
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        client = hub_client.HubSession("https://hub.example", "key", 0.01, 3)
        client._session = PendingSession()
        with pytest.raises(hub_client.HubConnectionError, match="TimeoutError"):
            await asyncio.wait_for(client.call("delete_trace", {"id": "one"}, retry_transport=False), 1)
        assert cancelled.is_set()
        assert len(calls) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("handshake_hangs", [False, True])
def test_session_cleanup_clears_the_handle_and_bounds_initialization(monkeypatch, handshake_hangs):
    import asyncio
    import contextlib

    async def exercise():
        closed = False
        exited = False
        cancelled = False

        class Transport:
            async def aclose(self):
                nonlocal closed
                closed = True

        @contextlib.asynccontextmanager
        async def wire():
            nonlocal exited
            try:
                yield None, None
            finally:
                exited = True

        class Session:
            def __init__(self, *_args):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def initialize(self):
                nonlocal cancelled
                if handshake_hangs:
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled = True

        async def open_wire(*_args):
            return wire(), Session, Transport(), hub_client.HttpStatusProbe()

        monkeypatch.setattr(hub_client, "_open_session", open_wire)
        if handshake_hangs:
            with pytest.raises(hub_client.HubConnectionError, match="TimeoutError"):
                async with hub_client.open_hub_session("https://hub.example", "key", 0.01):
                    pytest.fail("an unanswered handshake must not open a session")
            assert cancelled
        else:
            async with hub_client.open_hub_session("https://hub.example", "key") as client:
                assert client._session is not None
            with pytest.raises(hub_client.HubConnectionError, match="not open"):
                await client.call("get_trace", {})
        assert closed and exited

    asyncio.run(exercise())


def test_invalid_session_options_allocate_no_http_client(monkeypatch):
    import asyncio

    async def must_not_open(*_args):
        pytest.fail("invalid configuration allocated an HTTP client")

    monkeypatch.setattr(hub_client, "_open_session", must_not_open)

    async def exercise():
        with pytest.raises(hub_client.HubConfigurationError, match="max_attempts"):
            async with hub_client.open_hub_session("https://hub.example", "key", max_attempts=0):
                pytest.fail("invalid configuration opened a session")

    asyncio.run(exercise())


def test_comment_does_not_duplicate_a_committed_write_after_response_timeout(monkeypatch):
    import asyncio

    async def exercise():
        calls = []

        class Session:
            async def call_tool(self, name, arguments):
                calls.append((name, arguments))
                raise TimeoutError("response read timeout after commit")

        session = hub_client.HubSession("https://hub.example", "key", 30, 3)
        session._session = Session()
        with pytest.raises(hub_client.HubConnectionError):
            await session.add_comment("trace-one", "review")
        assert len(calls) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("payload", [[], ["one"], None, True, 12, "value"])
@pytest.mark.parametrize("structured", [True, False])
def test_nonobject_tool_responses_fail_at_the_transport_boundary(payload, structured):
    import json
    from types import SimpleNamespace

    result = SimpleNamespace(
        is_error=False,
        structured_content=payload if structured else None,
        content=[] if structured else [SimpleNamespace(text=json.dumps(payload))],
    )
    if structured and payload is None:
        # None marks absence of structured content in the MCP protocol.
        assert hub_client._unwrap_result("test", result) == {}
    else:
        with pytest.raises(hub_client.HubToolError, match="JSON object"):
            hub_client._unwrap_result("test", result)


def test_concurrent_rpc_failures_keep_their_own_http_status():
    import asyncio
    from types import SimpleNamespace

    async def exercise():
        probe = hub_client.HttpStatusProbe()
        slow_started = asyncio.Event()
        auth_rejected = asyncio.Event()

        class ConcurrentSession:
            async def call_tool(self, name, _arguments):
                if name == "slow":
                    slow_started.set()
                    await auth_rejected.wait()
                    raise TimeoutError("response timeout")
                await slow_started.wait()
                await probe.record(SimpleNamespace(status_code=401, headers={}))
                auth_rejected.set()
                raise RuntimeError("Server returned an error response")

        client = hub_client.HubSession("https://hub.example", "key", 1, 1, probe=probe)
        client._session = ConcurrentSession()
        slow, unauthorized = await asyncio.gather(
            client.call("slow", {}), client.call("unauthorized", {}), return_exceptions=True
        )
        assert isinstance(slow, hub_client.HubConnectionError)
        assert not isinstance(slow, hub_client.HubAuthError)
        assert isinstance(unauthorized, hub_client.HubAuthError)

    asyncio.run(exercise())


def test_native_mcp_transport_carries_per_call_http_observations(monkeypatch):
    import asyncio
    import json

    httpx = pytest.importorskip("httpx2")
    streamable = pytest.importorskip("mcp.client.streamable_http")
    from mcp import ClientSession

    async def exercise():
        probe = hub_client.HttpStatusProbe()
        slow_started = asyncio.Event()
        auth_rejected = asyncio.Event()

        async def handle(request):
            if request.method == "GET":
                return httpx.Response(405)
            payload = json.loads(request.content)
            if payload["method"] == "initialize":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "protocolVersion": payload["params"]["protocolVersion"],
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "audit", "version": "1"},
                        },
                    },
                )
            if payload["method"] == "notifications/initialized":
                return httpx.Response(202)
            if payload["method"] == "tools/call":
                if payload["params"]["name"] == "slow":
                    slow_started.set()
                    await auth_rejected.wait()
                    return httpx.Response(503, text="unavailable")
                await slow_started.wait()
                auth_rejected.set()
                return httpx.Response(401, text="unauthorized")
            return httpx.Response(202)

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle), event_hooks={"response": [probe.record]})

        async def open_wire(*_args):
            return (
                streamable.streamable_http_client("https://hub.example/mcp", http_client=http_client),
                ClientSession,
                http_client,
                probe,
            )

        monkeypatch.setattr(hub_client, "_open_session", open_wire)
        async with hub_client.open_hub_session("https://hub.example/mcp", "key", 1, 1) as client:
            slow, unauthorized = await asyncio.gather(
                client.call("slow", {}), client.call("unauthorized", {}), return_exceptions=True
            )
            assert type(slow) is hub_client.HubConnectionError
            assert isinstance(unauthorized, hub_client.HubAuthError)
        assert http_client.is_closed

    asyncio.run(asyncio.wait_for(exercise(), 5))
