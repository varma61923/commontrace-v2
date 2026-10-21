"""Provider-free native LangGraph lifecycle checks; optional SDK dependency.

The graph, message reducer, tool dispatcher and checkpointer are real. The chat
model is deterministic, so these checks establish no provider transport claims.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("langgraph.prebuilt", reason="LangGraph is an optional integration dependency")

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.prebuilt import create_react_agent  # noqa: E402
from pydantic import Field  # noqa: E402

from unimem import holdout_io  # noqa: E402
from unimem.integrations.langgraph import (  # noqa: E402
    create_pre_model_hook,
    record_outcome,
    turn_occasion_id,
)
from unimem.measure import CausalMemory  # noqa: E402


class _ToolModel(BaseChatModel):
    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "unimem-provider-free-tool-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        if isinstance(messages[-1], ToolMessage):
            response = AIMessage(content="The tool completed.")
        else:
            response = AIMessage(content="", tool_calls=[{
                "name": "lookup", "args": {"value": "result"}, "id": f"tool-{len(self.seen)}", "type": "tool_call",
            }])
        return ChatResult(generations=[ChatGeneration(message=response)])


@tool
def lookup(value: str) -> str:
    """Look up a deterministic value without contacting a provider."""
    return value


def _scope(config):
    return {"tenant": config["configurable"]["tenant_id"], "user": config["configurable"]["user_id"]}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_tool_loop_scoped_recall_turn_outcome_and_checkpoint(tmp_path, asynchronous):
    holdout_io.configure(str(tmp_path), rate=0.5)
    calls = []

    def retrieve(query, **kwargs):
        calls.append((query, kwargs))
        return [{"id": "lesson-1", "memory": "Use idempotency keys."},
                {"id": "lesson-2", "memory": "Log the delivery result."}]

    memory = CausalMemory(retrieve, root=str(tmp_path), pinned=["lesson-1"])
    model = _ToolModel()
    agent = create_react_agent(
        model, [lookup], pre_model_hook=create_pre_model_hook(memory, recall_kwargs=_scope),
        checkpointer=InMemorySaver(), prompt="Stable system instructions.",
    )
    config = {"configurable": {"thread_id": "conversation", "tenant_id": "tenant-a", "user_id": "user-a"}}

    def invoke(data):
        return asyncio.run(agent.ainvoke(data, config)) if asynchronous else agent.invoke(data, config)

    first = invoke({"messages": [HumanMessage(content="Retry webhook?", id="turn-1")]})
    assert calls == [("Retry webhook?", {"tenant": "tenant-a", "user": "user-a"})]
    assert len(model.seen) == 2
    assert all(sum("UNIMEM_MEMORY" in str(message.content) for message in messages) == 1 for messages in model.seen)
    assert all(messages[0].content == "Stable system instructions." for messages in model.seen)
    assert all(isinstance(messages[1], SystemMessage) for messages in model.seen)
    assert [message.type for message in first["messages"]] == ["human", "ai", "tool", "ai"]
    assert record_outcome(memory, config, state=first, recall_kwargs=_scope, succeeded=True)

    second = invoke({"messages": [HumanMessage(content="Retry webhook?", id="turn-2")]})
    assert len(calls) == 2  # repeated text is a fresh turn, not a new occasion per tool call
    assert len(model.seen) == 4
    snapshot = agent.get_state(config)
    assert [message.type for message in snapshot.values["messages"]] == ["human", "ai", "tool", "ai"] * 2
    assert all("UNIMEM_MEMORY" not in str(message.content) for message in snapshot.values["messages"])
    assert record_outcome(memory, config, state=second, recall_kwargs=_scope, succeeded=False)
    expected = {
        turn_occasion_id(first, config, scope=_scope(config)): True,
        turn_occasion_id(second, config, scope=_scope(config)): False,
    }
    assert holdout_io.read_outcomes(str(tmp_path)) == expected
    with open(holdout_io.holdout_log_path(str(tmp_path)), encoding="utf-8") as stream:
        assignments = [json.loads(line) for line in stream]
    assert len(assignments) == 2
    assert {row["occasion_id"] for row in assignments} == set(expected)


def test_native_tool_loop_caches_fail_open_result_and_retries_next_turn(tmp_path):
    calls = []

    def unavailable(query, **kwargs):
        calls.append(query)
        raise RuntimeError("store down")

    model = _ToolModel()
    agent = create_react_agent(
        model, [lookup], pre_model_hook=create_pre_model_hook(CausalMemory(unavailable, root=str(tmp_path))),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "conversation"}}
    first = agent.invoke({"messages": [HumanMessage(content="hello", id="turn-1")]}, config)
    assert first["messages"][-1].content == "The tool completed."
    assert calls == ["hello"]
    agent.invoke({"messages": [HumanMessage(content="hello", id="turn-2")]}, config)
    assert calls == ["hello", "hello"]
    assert not any("UNIMEM_MEMORY" in str(message.content) for messages in model.seen for message in messages)
