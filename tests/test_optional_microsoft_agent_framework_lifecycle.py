"""Real Microsoft Agent runner/provider lifecycle with a deterministic local client.

Install agent-framework-core to run these tests. No model or provider transport
is exercised: the SDK owns message preparation, sessions and tool execution.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from unimem.client import MemoryClient
from unimem.integrations.microsoft_agent_framework import UniMemContextProvider
from unimem.memory_journal import Journal
from unimem.memory_record import MemoryRecord

af = pytest.importorskip("agent_framework", reason="optional agent-framework-core SDK")

pytestmark = pytest.mark.integration


class LocalClient(af.FunctionInvocationLayer, af.BaseChatClient):
    """Use the actual SDK pipeline while keeping the model boundary local."""

    def __init__(self, *, use_tool=False):
        super().__init__()
        self.requests = []
        self.use_tool = use_tool

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        assert not stream
        self.requests.append((list(messages), dict(options)))
        await asyncio.sleep(0)  # Interleave concurrent sessions through SDK hooks.
        if self.use_tool and not any(message.role == "tool" for message in messages):
            return af.ChatResponse(messages=[af.Message("assistant", [
                af.Content.from_function_call("release-check", "verify_release", arguments={}),
            ])])
        return af.ChatResponse(messages=[af.Message("assistant", ["Verified by the local test client."])])


def _buffer(root, scope, session):
    run = hashlib.sha256(json.dumps([scope, session.session_id], sort_keys=True).encode()).hexdigest()
    return Journal(str(root)).buffer(run, scope=scope)


def _provider(root):
    memory = MemoryClient(str(root), agent_id="af-agent", context={"tenant": "acme"})
    Journal(str(root)).put(MemoryRecord(
        "alice-release", "directive", 1, {"text": "Always verify the blue release gate."},
        scope={"agent": "af-agent", "tenant": "acme", "user": "alice"},
    ))
    return UniMemContextProvider(memory)


def test_real_agent_concurrent_sessions_inject_and_capture_scoped_turns(monkeypatch, tmp_path):
    async def _test():
        provider = _provider(tmp_path)
        client = LocalClient()
        agent = af.Agent(client, context_providers=[provider])
        sessions = {user: agent.create_session() for user in ("alice", "bob")}
        original_turn = MemoryClient.turn
        calls = []

        def tracked_turn(memory, session, query, **kwargs):
            calls.append((tuple(memory.context), session))
            return original_turn(memory, session, query, **kwargs)

        monkeypatch.setattr(MemoryClient, "turn", tracked_turn)
        responses = await asyncio.gather(*(
            agent.run("How should I verify the blue release gate?", session=session, options={"user_id": user})
            for user, session in sessions.items()
        ))

        assert isinstance(provider, af.ContextProvider)
        assert len(calls) == len(client.requests) == 2
        assert all(response.text == "Verified by the local test client." for response in responses)
        instructions = [options.get("instructions", "") for _, options in client.requests]
        assert sum("Always verify the blue release gate." in value for value in instructions) == 1
        for user, session in sessions.items():
            scope = {"agent": "af-agent", "tenant": "acme", "user": user}
            events = _buffer(tmp_path, scope, session)
            assert len([event for event in events if event["event"] == "UserMessage"]) == 1
            ends = [event for event in events if event["event"] == "SessionEnd"]
            assert len(ends) == 1
            assert ends[0]["payload"]["result"] == responses[0].text
            assert session.state[provider.source_id] == {}
            assert (tuple(sorted("%s:%s" % item for item in scope.items())), session.session_id) in [
                (tuple(sorted(labels)), identity) for labels, identity in calls
            ]
        # A second run uses the same session state; the first turn marker is gone.
        await agent.run("Continue verification", session=sessions["alice"], options={"user_id": "alice"})
        assert len(calls) == 3
        alice_events = _buffer(tmp_path, {"agent": "af-agent", "tenant": "acme", "user": "alice"}, sessions["alice"])
        assert len([event for event in alice_events if event["event"] == "SessionEnd"]) == 2

    asyncio.run(_test())


def test_real_agent_tool_round_trip_captures_once_after_completion(tmp_path):
    async def _test():
        provider = _provider(tmp_path)
        client = LocalClient(use_tool=True)
        tool_calls = []

        @af.tool
        def verify_release() -> str:
            """Verify the release gate locally."""
            tool_calls.append("verified")
            return "gate passed"

        agent = af.Agent(client, tools=[verify_release], context_providers=[provider])
        session = agent.create_session()
        response = await agent.run("Verify the blue release gate", session=session, options={"user_id": "alice"})

        assert tool_calls == ["verified"]
        assert len(client.requests) == 2
        assert any(message.role == "tool" for message in client.requests[-1][0])
        assert response.text == "Verified by the local test client."
        events = _buffer(tmp_path, {"agent": "af-agent", "tenant": "acme", "user": "alice"}, session)
        assert len([event for event in events if event["event"] == "UserMessage"]) == 1
        ends = [event for event in events if event["event"] == "SessionEnd"]
        assert len(ends) == 1
        assert ends[0]["payload"]["result"] == response.text

    asyncio.run(_test())


def test_real_agent_loop_keeps_original_user_turn_and_captures_final_response(monkeypatch, tmp_path):
    async def _test():
        from agent_framework._harness._loop import AgentLoopMiddleware

        provider = _provider(tmp_path)
        client = LocalClient()
        calls = []
        original_turn = MemoryClient.turn

        def tracked_turn(memory, session_id, query, **kwargs):
            calls.append(query)
            return original_turn(memory, session_id, query, **kwargs)

        monkeypatch.setattr(MemoryClient, "turn", tracked_turn)
        loop = AgentLoopMiddleware(
            lambda *, iteration, **kwargs: iteration < 2,
            inject_progress=False,
            return_final_only=True,
        )
        agent = af.Agent(client, context_providers=[provider], middleware=[loop])
        session = agent.create_session()
        query = "Verify the blue release gate"
        response = await agent.run(query, session=session, options={"user_id": "alice"})

        assert len(client.requests) == 2
        assert calls == [query]
        assert all("Always verify the blue release gate." in options.get("instructions", "")
                   for _, options in client.requests)
        events = _buffer(tmp_path, {"agent": "af-agent", "tenant": "acme", "user": "alice"}, session)
        users = [event for event in events if event["event"] == "UserMessage"]
        assert [event["payload"]["text"] for event in users] == [query]
        ends = [event for event in events if event["event"] == "SessionEnd"]
        assert len(ends) == 1
        assert ends[0]["payload"]["result"] == response.text
        assert session.state[provider.source_id] == {}

    asyncio.run(_test())


def test_real_agent_memory_outage_preserves_response(monkeypatch, tmp_path):
    async def _test():
        provider = _provider(tmp_path)
        client = LocalClient()
        agent = af.Agent(client, context_providers=[provider])
        session = agent.create_session()

        def unavailable(*args, **kwargs):
            raise RuntimeError("local memory unavailable")

        monkeypatch.setattr(MemoryClient, "turn", unavailable)
        response = await agent.run("Continue verification", session=session, options={"user_id": "alice"})
        assert response.text == "Verified by the local test client."
        assert len(client.requests) == 1
        assert "Always verify" not in client.requests[0][1].get("instructions", "")
        assert session.state[provider.source_id] == {}

    asyncio.run(_test())


def test_real_agent_rejects_missing_identity_before_memory_or_model(monkeypatch, tmp_path):
    async def _test():
        provider = _provider(tmp_path)
        client = LocalClient()
        agent = af.Agent(client, context_providers=[provider])

        def unscoped_request(*args, **kwargs):
            pytest.fail("missing identity must not reach UniMem")

        monkeypatch.setattr(MemoryClient, "turn", unscoped_request)
        with pytest.raises(ValueError, match="requires user_id"):
            await agent.run("Continue verification", session=agent.create_session())
        assert client.requests == []

    asyncio.run(_test())
