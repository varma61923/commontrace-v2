import builtins
import hashlib
import importlib
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from commontrace.client import MemoryClient
from commontrace.memory_journal import Journal
from commontrace.memory_record import MemoryRecord


def _install_fake_agent_framework(monkeypatch, request):
    module_name = "commontrace.integrations.microsoft_agent_framework"
    previous = sys.modules.pop(module_name, None)

    class ContextProvider:
        def __init__(self, *, source_id):
            self.source_id = source_id

    sdk = ModuleType("agent_framework")
    sdk.ContextProvider = ContextProvider
    monkeypatch.setitem(sys.modules, "agent_framework", sdk)
    module = importlib.import_module(module_name)

    def cleanup():
        sys.modules.pop(module_name, None)
        if previous is not None:
            sys.modules[module_name] = previous

    request.addfinalizer(cleanup)
    return module


class FakeMessage:
    def __init__(self, role, text):
        self.role = role
        self.text = text


class FakeSessionContext:
    def __init__(self, *, session_id, user_id, query):
        self.session_id = session_id
        self.options = {"user_id": user_id}
        self.input_messages = [FakeMessage("user", query)]
        self.response = None
        self.instructions = {}

    def extend_instructions(self, source_id, text):
        self.instructions[source_id] = text


def _run_key(scope, session_id):
    normalized = {key: scope[key] for key in ("agent", "tenant", "user") if key in scope}
    return hashlib.sha256(json.dumps([normalized, session_id], sort_keys=True).encode()).hexdigest()


def test_import_is_safe_without_optional_agent_framework(monkeypatch):
    module_name = "commontrace.integrations.microsoft_agent_framework"
    previous = sys.modules.pop(module_name, None)
    original_import = builtins.__import__

    def reject_agent_framework(name, *args, **kwargs):
        if name == "agent_framework" or name.startswith("agent_framework."):
            raise ModuleNotFoundError("optional dependency absent", name="agent_framework")
        return original_import(name, *args, **kwargs)

    try:
        monkeypatch.setattr(builtins, "__import__", reject_agent_framework)
        module = importlib.import_module(module_name)
        assert callable(module.CommonTraceContextProvider)
        with pytest.raises(ImportError, match="agent-framework-core"):
            module.CommonTraceContextProvider(object())
    finally:
        sys.modules.pop(module_name, None)
        if previous is not None:
            sys.modules[module_name] = previous


@pytest.mark.asyncio
async def test_context_provider_uses_scoped_one_call_lifecycle_and_isolates_users(
    monkeypatch, request, tmp_path
):
    module = _install_fake_agent_framework(monkeypatch, request)
    memory = MemoryClient(str(tmp_path), agent_id="af-agent", context={"tenant": "acme"})
    Journal(str(tmp_path)).put(
        MemoryRecord(
            "alice-release-directive",
            "directive",
            1,
            {"text": "Always verify the blue release gate."},
            scope={"agent": "af-agent", "tenant": "acme", "user": "alice"},
        )
    )

    original_turn = MemoryClient.turn
    calls = []

    def tracked_turn(client, session_id, query, **kwargs):
        result = original_turn(client, session_id, query, **kwargs)
        calls.append((tuple(client.context), session_id, query, kwargs))
        return result

    monkeypatch.setattr(MemoryClient, "turn", tracked_turn)
    provider = module.CommonTraceContextProvider(memory)
    alice_state, bob_state = {}, {}
    query = "How should I verify the blue release gate?"
    alice = FakeSessionContext(session_id="alice-run", user_id="alice", query=query)
    bob = FakeSessionContext(session_id="bob-run", user_id="bob", query=query)

    await provider.before_run(agent=None, session=SimpleNamespace(session_id="alice-run"), context=alice, state=alice_state)
    await provider.before_run(agent=None, session=SimpleNamespace(session_id="bob-run"), context=bob, state=bob_state)

    assert len(calls) == 2
    assert {"tenant:acme", "agent:af-agent", "user:alice"} <= set(calls[0][0])
    assert calls[0][1:] == ("alice-run", query, {"text": query, "budget": 600})
    assert isinstance(provider, sys.modules["agent_framework"].ContextProvider)
    assert "blue release gate" in alice.instructions[provider.source_id]
    assert provider.source_id not in bob.instructions

    alice_response = SimpleNamespace(messages=[FakeMessage("assistant", "I will verify the blue release gate.")])
    bob_response = SimpleNamespace(messages=[FakeMessage("assistant", "I have no scoped release preference.")])
    alice.response, bob.response = alice_response, bob_response
    await provider.after_run(agent=None, session=SimpleNamespace(session_id="alice-run"), context=alice, state=alice_state)
    await provider.after_run(agent=None, session=SimpleNamespace(session_id="bob-run"), context=bob, state=bob_state)

    alice_scope = {"agent": "af-agent", "tenant": "acme", "user": "alice"}
    alice_buffer = Journal(str(tmp_path)).buffer(_run_key(alice_scope, "alice-run"), scope=alice_scope)
    assert any(event["event"] == "UserMessage" for event in alice_buffer)
    assert any(
        event["event"] == "SessionEnd"
        and event["payload"].get("result") == "I will verify the blue release gate."
        for event in alice_buffer
    )
    bob_scope = {"agent": "af-agent", "tenant": "acme", "user": "bob"}
    assert not any(
        entry["payload"].get("result") == "I will verify the blue release gate."
        for entry in Journal(str(tmp_path)).buffer(_run_key(bob_scope, "bob-run"), scope=bob_scope)
        if entry["event"] == "SessionEnd"
    )
    assert alice.response is alice_response
    assert provider.after_run_once_per_turn is True


@pytest.mark.asyncio
async def test_memory_outages_fail_open_before_and_after_run(monkeypatch, request, tmp_path):
    module = _install_fake_agent_framework(monkeypatch, request)
    provider = module.CommonTraceContextProvider(
        MemoryClient(str(tmp_path), agent_id="af-agent", context={"user": "alice"})
    )
    response = SimpleNamespace(messages=[FakeMessage("assistant", "The response remains unchanged.")])
    context = FakeSessionContext(session_id="run-1", user_id="alice", query="Continue the task")
    state = {}
    original_turn = MemoryClient.turn

    def fail_turn(self, session_id, query, **kwargs):
        raise RuntimeError("memory unavailable")

    monkeypatch.setattr(MemoryClient, "turn", fail_turn)
    await provider.before_run(agent=None, session=SimpleNamespace(session_id="run-1"), context=context, state=state)
    assert context.instructions == {}
    context.response = response
    await provider.after_run(agent=None, session=SimpleNamespace(session_id="run-1"), context=context, state=state)
    assert context.response is response
    assert state == {}

    monkeypatch.setattr(MemoryClient, "turn", original_turn)
    state = {}
    context.response = None
    await provider.before_run(agent=None, session=SimpleNamespace(session_id="run-1"), context=context, state=state)
    context.response = response

    from commontrace.framework_middleware import Middleware

    def fail_capture(self, outcome):
        raise RuntimeError("memory unavailable")

    monkeypatch.setattr(Middleware, "after_run", fail_capture)
    await provider.after_run(agent=None, session=SimpleNamespace(session_id="run-1"), context=context, state=state)
    assert context.response is response
    assert state == {}


@pytest.mark.asyncio
async def test_missing_user_identity_does_not_run_unscoped_memory(monkeypatch, request, tmp_path):
    module = _install_fake_agent_framework(monkeypatch, request)
    provider = module.CommonTraceContextProvider(MemoryClient(str(tmp_path), agent_id="af-agent"))
    context = FakeSessionContext(session_id="run-1", user_id="", query="Do not use unscoped memory")
    context.options.clear()

    with pytest.raises(ValueError, match="requires user_id"):
        await provider.before_run(agent=None, session=SimpleNamespace(session_id="run-1"), context=context, state={})
