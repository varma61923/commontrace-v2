from __future__ import annotations

import asyncio
import builtins
import importlib
import sys
import threading
import time
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Generic, TypeVar

import pytest

T = TypeVar("T")


@dataclass
class FakeSystemPromptPart:
    content: str
    part_kind: str = "system-prompt"


@dataclass
class FakeUserPromptPart:
    content: str
    part_kind: str = "user-prompt"


@dataclass
class FakeModelRequest:
    parts: list[Any]
    run_id: str | None = None
    conversation_id: str | None = None


class FakeRunContext(Generic[T]):
    def __init__(self, run_id: str, *, conversation_id: str | None = None):
        self.run_id = run_id
        self.conversation_id = conversation_id


class FakeProcessHistory:
    def __init__(self, processor):
        self.processor = processor


class FakeHooks:
    def __init__(self, *, after_run):
        self.after_run = after_run


def _install_fake_sdk(monkeypatch):
    package = ModuleType("pydantic_ai")
    package.RunContext = FakeRunContext
    capabilities = ModuleType("pydantic_ai.capabilities")
    capabilities.ProcessHistory = FakeProcessHistory
    capabilities.Hooks = FakeHooks
    messages = ModuleType("pydantic_ai.messages")
    messages.ModelRequest = FakeModelRequest
    messages.SystemPromptPart = FakeSystemPromptPart
    monkeypatch.setitem(sys.modules, "pydantic_ai", package)
    monkeypatch.setitem(sys.modules, "pydantic_ai.capabilities", capabilities)
    monkeypatch.setitem(sys.modules, "pydantic_ai.messages", messages)


class FakeMemory:
    def __init__(self, user: str, *, fail_read: bool = False, fail_write: bool = False):
        self.context = [f"user:{user}"]
        self.agent_id = "support"
        self.url = "https://gateway.invalid"
        self.root = "."
        self.fail_read = fail_read
        self.fail_write = fail_write
        self.calls = []

    def turn(self, session, query, **kwargs):
        self.calls.append((session, query, kwargs))
        if kwargs.get("event"):
            if self.fail_write:
                raise RuntimeError("memory write unavailable")
            return {"recorded": True}
        if self.fail_read:
            raise RuntimeError("memory read unavailable")
        user = self.context[0].split(":", 1)[1]
        return {"context": f"{user} preference: concise answers"}


def _user_history(text: str, conversation_id: str | None = "chat-1"):
    return [FakeModelRequest([FakeUserPromptPart(text)], conversation_id=conversation_id)]


def _parts(messages, kind):
    return [part for message in messages for part in getattr(message, "parts", []) if part.part_kind == kind]


def test_integration_module_imports_without_the_optional_sdk():
    # The adapter module itself must remain usable to dependency-light clients.
    module = importlib.import_module("commontrace.integrations.pydantic_ai")
    assert callable(module.memory_capabilities)


def test_capabilities_dedupe_each_run_inject_context_as_system_and_capture_result(monkeypatch):
    _install_fake_sdk(monkeypatch)
    from commontrace.integrations.pydantic_ai import memory_capabilities

    memory = FakeMemory("alice")
    process_history, after_run = memory_capabilities(memory, budget=123)
    process = process_history.processor
    after = after_run.after_run
    ctx = FakeRunContext("run-1", conversation_id="chat-1")
    original = _user_history("What style do I prefer?")

    first = asyncio.run(process(ctx, original))
    second = asyncio.run(process(ctx, first))

    assert len([call for call in memory.calls if not call[2].get("event")]) == 1
    assert len(_parts(second, "system-prompt")) == 1
    assert _parts(second, "system-prompt")[0].content.endswith("alice preference: concise answers")
    assert _parts(second, "user-prompt")[0].content == "What style do I prefer?"
    assert memory.calls[0] == (
        "chat-1",
        "What style do I prefer?",
        {"text": "What style do I prefer?", "budget": 123},
    )

    result = type("Result", (), {"output": "Use concise answers."})()
    returned = asyncio.run(after(ctx, result=result))
    assert returned is result
    assert memory.calls[-1][0:2] == ("chat-1", "")
    assert memory.calls[-1][2]["event"] == "SessionEnd"
    assert memory.calls[-1][2]["payload"] == {"succeeded": True, "result": "Use concise answers."}


def test_distinct_run_and_user_scopes_do_not_share_cached_context(monkeypatch):
    _install_fake_sdk(monkeypatch)
    from commontrace.integrations.pydantic_ai import memory_capabilities

    alice = FakeMemory("alice")
    bob = FakeMemory("bob")
    alice_process = memory_capabilities(alice)[0].processor
    bob_process = memory_capabilities(bob)[0].processor
    history = _user_history("Same question")

    alice_messages = asyncio.run(alice_process(FakeRunContext("run-a", conversation_id="chat-a"), history))
    bob_messages = asyncio.run(bob_process(FakeRunContext("run-b", conversation_id="chat-b"), history))
    repeated_run_messages = asyncio.run(alice_process(FakeRunContext("run-a-2", conversation_id="chat-a"), history))

    assert _parts(alice_messages, "system-prompt")[0].content.endswith("alice preference: concise answers")
    assert _parts(bob_messages, "system-prompt")[0].content.endswith("bob preference: concise answers")
    assert _parts(repeated_run_messages, "system-prompt")[0].content.endswith("alice preference: concise answers")
    assert len(alice.calls) == 2
    assert len(bob.calls) == 1


def test_memory_failures_leave_history_and_result_unchanged(monkeypatch):
    _install_fake_sdk(monkeypatch)
    from commontrace.integrations.pydantic_ai import memory_capabilities

    memory = FakeMemory("alice", fail_read=True, fail_write=True)
    process_history, hooks = memory_capabilities(memory)
    history = _user_history("hello")
    ctx = FakeRunContext("run-3", conversation_id="chat-3")

    output_history = asyncio.run(process_history.processor(ctx, history))
    retried_history = asyncio.run(process_history.processor(ctx, output_history))
    result = type("Result", (), {"output": "hello back"})()
    returned = asyncio.run(hooks.after_run(ctx, result=result))

    assert output_history == history
    assert retried_history == history
    assert returned is result
    # A failed read is cached for this run, and the final write is attempted once.
    assert len(memory.calls) == 2


def test_stalled_memory_reads_and_capture_return_within_hook_deadlines(monkeypatch):
    _install_fake_sdk(monkeypatch)
    from commontrace.integrations.pydantic_ai import memory_capabilities

    class StalledMemory(FakeMemory):
        def __init__(self):
            super().__init__("alice")
            self.entered = threading.Event()
            self.release = threading.Event()

        def turn(self, session, query, **kwargs):
            self.calls.append((session, query, kwargs))
            self.entered.set()
            self.release.wait()
            return {"context": "late memory result"}

    memory = StalledMemory()
    process_history, hooks = memory_capabilities(memory)
    ctx = FakeRunContext("run-stalled", conversation_id="chat-stalled")
    history = _user_history("hello")
    result = type("Result", (), {"output": "hello back"})()

    async def exercise():
        started = time.monotonic()
        output_history = await process_history.processor(ctx, history)
        retrieval_elapsed = time.monotonic() - started
        assert memory.entered.is_set()
        assert output_history == history

        # The first timed-out attempt is cached, so the next model request does
        # not start another read or wait again.
        calls_before_retry = len(memory.calls)
        retry_started = time.monotonic()
        retry_history = await process_history.processor(ctx, output_history)
        retry_elapsed = time.monotonic() - retry_started
        assert retry_history == history
        assert len(memory.calls) == calls_before_retry

        capture_started = time.monotonic()
        returned = await hooks.after_run(ctx, result=result)
        capture_elapsed = time.monotonic() - capture_started
        assert returned is result
        assert len(memory.calls) == 2
        memory.release.set()
        return retrieval_elapsed, retry_elapsed, capture_elapsed

    try:
        retrieval_elapsed, retry_elapsed, capture_elapsed = asyncio.run(exercise())
    finally:
        memory.release.set()

    assert retrieval_elapsed < 0.2
    assert retry_elapsed < 0.1
    assert capture_elapsed < 0.5


def test_memory_wait_limits_are_configurable_and_must_be_positive(monkeypatch):
    _install_fake_sdk(monkeypatch)
    from commontrace.integrations.pydantic_ai import memory_capabilities

    assert len(memory_capabilities(FakeMemory("alice"), retrieval_timeout=0.01, capture_timeout=0.02)) == 2
    with pytest.raises(ValueError, match="retrieval_timeout"):
        memory_capabilities(FakeMemory("alice"), retrieval_timeout=0)
    with pytest.raises(ValueError, match="capture_timeout"):
        memory_capabilities(FakeMemory("alice"), capture_timeout=float("inf"))


def test_missing_sdk_is_reported_only_when_constructing_capabilities(monkeypatch):
    from commontrace.integrations import pydantic_ai as ct

    original_import = builtins.__import__

    def reject_pydantic_ai(name, *args, **kwargs):
        if name == "pydantic_ai" or name.startswith("pydantic_ai."):
            raise ModuleNotFoundError("optional dependency absent", name="pydantic_ai")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_pydantic_ai)
    with pytest.raises(ImportError, match="pydantic-ai"):
        ct.memory_capabilities(FakeMemory("alice"))
