"""Tests for the LangGraph prebuilt ``pre_model_hook`` contract.

These tests intentionally do not require the optional LangGraph or LangChain
packages. The generated callback follows their documented state/config and
``llm_input_messages`` contract directly.
"""

from __future__ import annotations

from commontrace import holdout_io
from commontrace.integrations.langgraph import create_pre_model_hook
from commontrace.measure import CausalMemory


def _message_content(message):
    if isinstance(message, dict):
        return message["content"]
    return message.content


def test_pre_model_hook_injects_context_without_mutating_graph_state(tmp_path):
    holdout_io.configure(str(tmp_path), rate=0.0)
    calls = []

    def retrieve(query, **kwargs):
        calls.append((query, kwargs))
        return [{"id": "lesson-1", "memory": "Use idempotency keys for webhook retries."}]

    memory = CausalMemory(retrieve, root=str(tmp_path))
    user_message = {"role": "user", "content": "How should retries work?"}
    state = {"messages": [user_message], "other_state": "preserved"}
    hook = create_pre_model_hook(
        memory,
        recall_kwargs=lambda config: {"tenant": config["configurable"]["tenant_id"]},
    )

    update = hook(
        state,
        {"configurable": {"thread_id": "thread-a", "tenant_id": "tenant-a"}},
    )

    assert set(update) == {"llm_input_messages"}
    injected, forwarded = update["llm_input_messages"]
    assert "idempotency keys" in _message_content(injected)
    assert forwarded is user_message
    assert state == {"messages": [user_message], "other_state": "preserved"}
    assert calls == [("How should retries work?", {"tenant": "tenant-a"})]


def test_pre_model_hook_uses_thread_id_as_causal_occasion_and_skips_unscoped_reads():
    calls = []

    class RecordingMemory:
        def recall(self, query, *, occasion_id, **kwargs):
            calls.append((query, occasion_id, kwargs))
            return []

    hook = create_pre_model_hook(RecordingMemory())
    state = {"messages": [{"role": "user", "content": "hello"}]}

    update = hook(state, {"configurable": {"thread_id": "thread-b"}})
    assert update == {"llm_input_messages": state["messages"]}
    assert calls == [("hello", "thread-b", {})]

    # A missing thread id must not turn into an unscoped retrieval.
    assert hook(state) == {"llm_input_messages": state["messages"]}
    assert len(calls) == 1


def test_pre_model_hook_fails_open_when_memory_is_unavailable(tmp_path):
    def retrieve(_query, **_kwargs):
        raise RuntimeError("memory store is unavailable")

    hook = create_pre_model_hook(CausalMemory(retrieve, root=str(tmp_path)))
    user_message = {"role": "user", "content": "hello"}
    state = {"messages": [user_message]}

    update = hook(state, {"configurable": {"thread_id": "thread-c"}})

    assert update == {"llm_input_messages": [user_message]}
    assert state["messages"] == [user_message]
