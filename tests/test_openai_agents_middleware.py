from __future__ import annotations

import asyncio
import builtins
import importlib
import sys
import threading
import time
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace

import pytest

from commontrace.client import MemoryClient
from commontrace.memory_journal import Journal
from commontrace.memory_record import MemoryRecord


def _install_fake_agents(monkeypatch, request):
    module_name = "commontrace.integrations.openai_agents"
    previous_adapter = sys.modules.pop(module_name, None)

    class RunHooks:
        pass

    @dataclass
    class ModelInputData:
        input: list
        instructions: str | None

    agents = ModuleType("agents")
    agents.RunHooks = RunHooks
    config = ModuleType("agents.run_config")
    config.ModelInputData = ModelInputData
    monkeypatch.setitem(sys.modules, "agents", agents)
    monkeypatch.setitem(sys.modules, "agents.run_config", config)

    module = importlib.import_module(module_name)

    def cleanup():
        sys.modules.pop(module_name, None)
        if previous_adapter is not None:
            sys.modules[module_name] = previous_adapter

    request.addfinalizer(cleanup)
    return module, ModelInputData, RunHooks


def _request(ModelInputData, context, items, instructions="Be useful."):
    model_data = ModelInputData(input=items, instructions=instructions)
    return SimpleNamespace(model_data=model_data, context=context, agent=SimpleNamespace(name="assistant"))


def test_openai_agents_adapter_import_is_safe_without_optional_sdk(monkeypatch, tmp_path):
    module_name = "commontrace.integrations.openai_agents"
    previous = sys.modules.pop(module_name, None)
    original_import = builtins.__import__

    def reject_agents(name, *args, **kwargs):
        if name == "agents" or name.startswith("agents."):
            raise ModuleNotFoundError("optional dependency absent", name="agents")
        return original_import(name, *args, **kwargs)

    try:
        monkeypatch.setattr(builtins, "__import__", reject_agents)
        module = importlib.import_module(module_name)
        assert callable(module.OpenAIAgentsMemory)
        assert not (tmp_path / "memory").exists()
    finally:
        sys.modules.pop(module_name, None)
        if previous is not None:
            sys.modules[module_name] = previous


@pytest.mark.asyncio
async def test_call_model_filter_uses_one_scoped_turn_and_hook_captures_final_output(monkeypatch, request, tmp_path):
    module, ModelInputData, RunHooks = _install_fake_agents(monkeypatch, request)
    memory = MemoryClient(str(tmp_path), agent_id="support", context={"tenant": "acme"})
    Journal(str(tmp_path)).put(
        MemoryRecord(
            "alice-policy",
            "directive",
            1,
            {"text": "Refunds take five business days."},
            scope={"agent": "support", "tenant": "acme", "user": "alice"},
        )
    )
    calls = []
    original_turn = MemoryClient.turn

    def tracked_turn(client, session_id, query, **kwargs):
        result = original_turn(client, session_id, query, **kwargs)
        calls.append((set(client.context), session_id, query, kwargs))
        return result

    monkeypatch.setattr(MemoryClient, "turn", tracked_turn)
    adapter = module.OpenAIAgentsMemory(
        memory,
        session_id=lambda context: context["session_id"],
        user_id=lambda context: context["user_id"],
        retrieval_timeout_ms=1000,
        capture_timeout_ms=1000,
    )
    context = {"session_id": "alice-session", "user_id": "alice"}
    first = _request(
        ModelInputData,
        context,
        [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
            {"role": "user", "content": [{"type": "input_text", "text": "How long is a refund?"}]},
        ],
    )

    injected = await adapter.call_model_input_filter(first)
    assert isinstance(injected, ModelInputData)
    assert "Refunds take five business days." in injected.instructions
    assert injected.input == first.model_data.input
    assert len(calls) == 1
    assert {"agent:support", "tenant:acme", "user:alice"} <= calls[0][0]
    assert calls[0][1:] == (
        "alice-session",
        "How long is a refund?",
        {"text": "How long is a refund?", "budget": 600},
    )

    # Tool-loop calls keep the same user turn and carry the same injected memory.
    second = _request(
        ModelInputData,
        context,
        [
            *first.model_data.input,
            {"type": "function_call", "name": "lookup", "arguments": "{}", "call_id": "call-1"},
            {"type": "function_call_output", "call_id": "call-1", "output": "policy checked"},
        ],
        instructions=injected.instructions,
    )
    continued = await adapter.call_model_input_filter(second)
    assert len(calls) == 1
    assert "Refunds take five business days." in continued.instructions
    assert continued.instructions.count("<COMMONTRACE_MEMORY>") == 1

    hooks = adapter.run_hooks()
    assert isinstance(hooks, RunHooks)
    output = "Refunds take five business days; I can check your request status too."
    await hooks.on_agent_end(SimpleNamespace(context=context), first.agent, output)
    assert len(calls) == 2
    assert calls[-1][1:] == (
        "alice-session",
        "",
        {
            "event": "SessionEnd",
            "payload": {"succeeded": True, "result": output},
            "budget": 0,
        },
    )
    assert calls[-1][0] == calls[0][0]


@pytest.mark.asyncio
async def test_openai_agents_adapter_isolates_user_scope_between_runs(monkeypatch, request, tmp_path):
    module, ModelInputData, _ = _install_fake_agents(monkeypatch, request)
    memory = MemoryClient(str(tmp_path), agent_id="support", context={"tenant": "acme"})
    Journal(str(tmp_path)).put(
        MemoryRecord(
            "alice-secret",
            "directive",
            1,
            {"text": "Alice prefers email updates."},
            scope={"agent": "support", "tenant": "acme", "user": "alice"},
        )
    )
    adapter = module.OpenAIAgentsMemory(
        memory,
        session_id=lambda context: context["session_id"],
        user_id=lambda context: context["user_id"],
        retrieval_timeout_ms=1000,
    )
    alice = await adapter.call_model_input_filter(
        _request(
            ModelInputData, {"session_id": "alice-s", "user_id": "alice"}, [{"role": "user", "content": "Continue"}]
        )
    )
    assert "Alice prefers email updates." in alice.instructions

    bob_adapter = module.OpenAIAgentsMemory(
        memory,
        session_id=lambda context: context["session_id"],
        user_id=lambda context: context["user_id"],
        retrieval_timeout_ms=1000,
    )
    bob = await bob_adapter.call_model_input_filter(
        _request(ModelInputData, {"session_id": "bob-s", "user_id": "bob"}, [{"role": "user", "content": "Continue"}])
    )
    assert "Alice prefers email updates." not in bob.instructions


@pytest.mark.asyncio
async def test_openai_agents_memory_failure_fails_open_once_per_run(monkeypatch, request, tmp_path):
    module, ModelInputData, _ = _install_fake_agents(monkeypatch, request)
    calls = []

    def fail_turn(self, session_id, query, **kwargs):
        calls.append((session_id, query))
        raise RuntimeError("memory unavailable")

    monkeypatch.setattr(MemoryClient, "turn", fail_turn)
    adapter = module.OpenAIAgentsMemory(
        MemoryClient(str(tmp_path), agent_id="support", context={"user": "alice"}),
        session_id="alice-session",
        retrieval_timeout_ms=1000,
    )
    model_data = _request(ModelInputData, None, [{"role": "user", "content": "Continue the task"}])

    first = await adapter.call_model_input_filter(model_data)
    second = await adapter.call_model_input_filter(model_data)
    assert first is model_data.model_data
    assert second is model_data.model_data
    assert calls == [("alice-session", "Continue the task")]

    hooks = adapter.run_hooks()
    output = object()
    await hooks.on_agent_end(SimpleNamespace(context=None), model_data.agent, output)
    assert output is not None
    assert calls == [("alice-session", "Continue the task")]


@pytest.mark.asyncio
async def test_openai_agents_capture_failure_does_not_escape_hook(monkeypatch, request, tmp_path):
    module, ModelInputData, _ = _install_fake_agents(monkeypatch, request)
    original_turn = MemoryClient.turn
    calls = []

    def fail_capture(client, session_id, query, **kwargs):
        calls.append((session_id, query, kwargs))
        if kwargs.get("event") == "SessionEnd":
            raise RuntimeError("memory unavailable while recording completion")
        return original_turn(client, session_id, query, **kwargs)

    monkeypatch.setattr(MemoryClient, "turn", fail_capture)
    adapter = module.OpenAIAgentsMemory(
        MemoryClient(str(tmp_path), agent_id="support", context={"user": "alice"}),
        session_id="alice-session",
        retrieval_timeout_ms=1000,
    )
    data = _request(ModelInputData, None, [{"role": "user", "content": "Keep the response unchanged"}])
    await adapter.call_model_input_filter(data)
    output = "The final response remains available to the caller."

    await adapter.run_hooks().on_agent_end(SimpleNamespace(context=None), data.agent, output)

    assert len(calls) == 2
    assert calls[-1][2]["payload"] == {"succeeded": True, "result": output}


@pytest.mark.asyncio
async def test_slow_openai_agents_capture_returns_within_bound_and_preserves_output(monkeypatch, request, tmp_path):
    module, ModelInputData, _ = _install_fake_agents(monkeypatch, request)
    original_turn = MemoryClient.turn
    capture_started = threading.Event()
    release_capture = threading.Event()
    capture_finished = threading.Event()

    def slow_capture(client, session_id, query, **kwargs):
        if kwargs.get("event") == "SessionEnd":
            capture_started.set()
            release_capture.wait(timeout=2)
            capture_finished.set()
            return {}
        return original_turn(client, session_id, query, **kwargs)

    monkeypatch.setattr(MemoryClient, "turn", slow_capture)
    adapter = module.OpenAIAgentsMemory(
        MemoryClient(str(tmp_path), agent_id="support", context={"user": "alice"}),
        session_id="alice-session",
        retrieval_timeout_ms=1000,
    )
    assert adapter.capture_timeout_ms == 40
    data = _request(ModelInputData, None, [{"role": "user", "content": "Finish promptly"}])
    await adapter.call_model_input_filter(data)
    output = "The completed answer remains available immediately."
    started_at = time.monotonic()

    try:
        await asyncio.wait_for(
            adapter.run_hooks().on_agent_end(SimpleNamespace(context=None), data.agent, output),
            timeout=0.5,
        )
        elapsed = time.monotonic() - started_at
        assert capture_started.is_set()
        assert not release_capture.is_set()
        assert not capture_finished.is_set()
        assert elapsed < 0.2  # 40 ms configured bound plus scheduler margin
        assert output == "The completed answer remains available immediately."
    finally:
        release_capture.set()

    assert await asyncio.to_thread(capture_finished.wait, 1)
