"""commontrace/integrations/pydantic_ai.py and openai_agents.py, run as
real agent loops with offline models: Pydantic AI's TestModel, and a
scripted Model for the OpenAI Agents SDK. Skipped when the framework is
not installed."""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from commontrace import holdout_io
from commontrace.measure import CausalMemory


class FakeStore:
    def __init__(self):
        self.queries = []

    def search(self, query, **kwargs):
        self.queries.append(query)
        return [{"id": "m1", "memory": "refunds take five days"}]


def _log_occasions(root):
    path = holdout_io.holdout_log_path(str(root))
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line)["occasion_id"] for line in fh if line.strip()]


class TestPydanticAI:
    @pytest.fixture(autouse=True)
    def _need(self, monkeypatch):
        pytest.importorskip("pydantic_ai")
        monkeypatch.setenv("PYDANTIC_AI_NO_BANNER", "1")

    def test_the_tool_recalls_under_the_runs_conversation_id(self, tmp_path):
        from pydantic_ai import Agent
        from pydantic_ai.models.test import TestModel

        from commontrace.integrations import pydantic_ai as ct

        holdout_io.configure(str(tmp_path), rate=0.5)
        store = FakeStore()
        memory = CausalMemory(store.search, root=str(tmp_path))
        agent = Agent(TestModel(), tools=[ct.memory_tool(memory)])
        result = agent.run_sync("a customer asks about a refund")

        assert store.queries
        assert set(_log_occasions(tmp_path)) == {result.conversation_id}
        assert ct.record_outcome(memory, result, succeeded=True) is True
        assert holdout_io.read_outcomes(str(tmp_path)) == {result.conversation_id: True}

    def test_run_id_can_be_the_occasion_instead(self, tmp_path):
        from pydantic_ai import Agent
        from pydantic_ai.models.test import TestModel

        from commontrace.integrations import pydantic_ai as ct

        holdout_io.configure(str(tmp_path), rate=0.5)
        memory = CausalMemory(FakeStore().search, root=str(tmp_path))
        agent = Agent(TestModel(), tools=[ct.memory_tool(memory, occasion="run_id")])
        result = agent.run_sync("q")
        assert set(_log_occasions(tmp_path)) == {result.run_id}

    def test_a_broken_memory_does_not_fail_the_run(self, tmp_path):
        from pydantic_ai import Agent
        from pydantic_ai.models.test import TestModel

        from commontrace.integrations import pydantic_ai as ct

        def boom(query, **kwargs):
            raise RuntimeError("store down")

        agent = Agent(TestModel(), tools=[ct.memory_tool(CausalMemory(boom, root=str(tmp_path)))])
        assert agent.run_sync("q").output is not None


class TestOpenAIAgents:
    @pytest.fixture(autouse=True)
    def _need(self):
        agents = pytest.importorskip("agents")
        yield
        agents.set_tracing_disabled(False)

    @staticmethod
    def _tracing(enabled):
        # The SDK reads OPENAI_AGENTS_DISABLE_TRACING once per process, so
        # set it through the SDK's own switch instead of the environment.
        import agents

        agents.set_tracing_disabled(not enabled)

    def _run(self, memory, *, context=None, group_id=None):
        from agents import Agent, Model, ModelResponse, RunConfig, Runner, Usage
        from openai.types.responses import (
            ResponseFunctionToolCall,
            ResponseOutputMessage,
            ResponseOutputText,
        )

        from commontrace.integrations import openai_agents as ct

        class Scripted(Model):
            turn = 0

            async def get_response(self, *args, **kwargs):
                self.turn += 1
                if self.turn == 1:
                    output = [ResponseFunctionToolCall(
                        type="function_call", call_id="c1", name="recall_memory",
                        arguments='{"query": "refund"}', id="fc1", status="completed",
                    )]
                else:
                    output = [ResponseOutputMessage(
                        type="message", id="m1", role="assistant", status="completed",
                        content=[ResponseOutputText(type="output_text", text="done", annotations=[])],
                    )]
                return ModelResponse(output=output, usage=Usage(), response_id=None)

            def stream_response(self, *args, **kwargs):
                raise NotImplementedError

        agent = Agent(name="a", model=Scripted(), tools=[ct.memory_tool(memory)])
        return asyncio.run(Runner.run(
            agent, "help", context=context, run_config=RunConfig(group_id=group_id),
        ))

    def test_the_occasion_comes_from_the_run_context_even_with_tracing_off(self, tmp_path):
        self._tracing(False)
        holdout_io.configure(str(tmp_path), rate=0.5)
        store = FakeStore()
        result = self._run(CausalMemory(store.search, root=str(tmp_path)), context={"occasion_id": "task-7"})
        assert result.final_output == "done"
        assert store.queries == ["refund"]
        assert set(_log_occasions(tmp_path)) == {"task-7"}

    def test_the_trace_group_id_is_the_fallback(self, tmp_path, monkeypatch):
        self._tracing(True)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        holdout_io.configure(str(tmp_path), rate=0.5)
        store = FakeStore()
        self._run(CausalMemory(store.search, root=str(tmp_path)), group_id="task-42")
        assert set(_log_occasions(tmp_path)) == {"task-42"}

    def test_no_occasion_means_no_memory_rather_than_an_invented_id(self, tmp_path):
        self._tracing(False)
        holdout_io.configure(str(tmp_path), rate=0.5)
        store = FakeStore()
        assert self._run(CausalMemory(store.search, root=str(tmp_path))).final_output == "done"
        assert store.queries == []
        assert _log_occasions(tmp_path) == []
