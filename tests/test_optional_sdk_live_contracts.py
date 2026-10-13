"""Credential-free contracts with the optional agent SDKs' public run APIs."""

from __future__ import annotations

import asyncio

import pytest

from commontrace.client import MemoryClient


def test_openai_agents_live_input_filter_and_end_hook(monkeypatch, tmp_path):
    async def _test():
        pytest.importorskip("agents")
        testing = pytest.importorskip("agents.testing", reason="requires the SDK's ScriptedModel test API")

        from agents import Agent, RunConfig, Runner

        from commontrace.integrations.openai_agents import OpenAIAgentsMemory

        calls = []

        def fake_turn(client, session_id, query, **kwargs):
            calls.append((set(client.context), session_id, query, kwargs))
            return {"context": "Refunds take five business days."}

        monkeypatch.setattr(MemoryClient, "turn", fake_turn)
        memory = MemoryClient(
            str(tmp_path),
            url="https://gateway.invalid",
            token="test-token",
            agent_id="support",
            context={"tenant": "acme"},
        )
        adapter = OpenAIAgentsMemory(
            memory,
            session_id=lambda context: context["session_id"],
            user_id=lambda context: context["user_id"],
        )
        model = testing.ScriptedModel([[testing.assistant_message("Refunds take five business days.")]])
        agent = Agent(name="support", model=model)

        result = await Runner.run(
            agent,
            "How long does a refund take?",
            context={"session_id": "session-alice", "user_id": "alice"},
            hooks=adapter.run_hooks(),
            run_config=RunConfig(
                call_model_input_filter=adapter.call_model_input_filter,
                tracing_disabled=True,
            ),
        )

        assert result.final_output == "Refunds take five business days."
        assert "Refunds take five business days." in model.first_call.system_instructions
        assert len(calls) == 2
        assert {"tenant:acme", "agent:support", "user:alice"} <= calls[0][0]
        assert calls[0][1:] == (
            "session-alice",
            "How long does a refund take?",
            {"text": "How long does a refund take?", "budget": 600},
        )
        assert calls[1][1:] == (
            "session-alice",
            "",
            {
                "event": "SessionEnd",
                "payload": {"succeeded": True, "result": result.final_output},
                "budget": 0,
            },
        )
        assert calls[1][0] == calls[0][0]
        model.assert_complete()

    asyncio.run(_test())


def test_pydantic_ai_live_process_history_and_after_run_capabilities(monkeypatch, tmp_path):
    async def _test():
        pytest.importorskip("pydantic_ai")
        pytest.importorskip("pydantic_ai.capabilities", reason="requires Pydantic AI capabilities")

        from pydantic_ai import Agent
        from pydantic_ai.messages import SystemPromptPart
        from pydantic_ai.models.test import TestModel

        from commontrace.integrations.pydantic_ai import memory_capabilities

        calls = []

        def fake_turn(client, session_id, query, **kwargs):
            calls.append((set(client.context), session_id, query, kwargs))
            return {"context": "Refunds take five business days."}

        monkeypatch.setattr(MemoryClient, "turn", fake_turn)
        memory = MemoryClient(
            str(tmp_path),
            url="https://gateway.invalid",
            token="test-token",
            agent_id="support",
            context={"tenant": "acme", "user": "alice"},
        )
        model = TestModel(custom_output_text="Refunds take five business days.")
        agent = Agent(model, capabilities=memory_capabilities(memory))

        result = await agent.run("How long does a refund take?")

        assert result.output == "Refunds take five business days."
        injected = [
            part.content
            for message in result.all_messages()
            for part in getattr(message, "parts", [])
            if isinstance(part, SystemPromptPart) and part.content.startswith("CommonTrace recalled evidence")
        ]
        assert len(injected) == 1
        assert "Refunds take five business days." in injected[0]
        assert len(calls) == 2
        assert {"tenant:acme", "agent:support", "user:alice"} <= calls[0][0]
        assert calls[0][1:] == (
            result.conversation_id,
            "How long does a refund take?",
            {"text": "How long does a refund take?", "budget": 600},
        )
        assert calls[1][1:] == (
            result.conversation_id,
            "",
            {
                "event": "SessionEnd",
                "payload": {"succeeded": True, "result": result.output},
                "budget": 0,
            },
        )

    asyncio.run(_test())
