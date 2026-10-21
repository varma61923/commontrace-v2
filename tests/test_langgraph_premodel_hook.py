"""Tests for the LangGraph prebuilt ``pre_model_hook`` contract.

These tests intentionally do not require the optional LangGraph or LangChain
packages. The generated callback follows their documented state/config and
``llm_input_messages`` contract directly.
"""

from __future__ import annotations

from unimem import holdout_io
from unimem.integrations.langgraph import create_pre_model_hook
from unimem.measure import CausalMemory


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


def test_pre_model_hook_uses_turn_identity_and_skips_unscoped_reads():
    calls = []

    class RecordingMemory:
        def recall(self, query, *, occasion_id, **kwargs):
            calls.append((query, occasion_id, kwargs))
            return []

    hook = create_pre_model_hook(RecordingMemory())
    state = {"messages": [{"role": "user", "content": "hello"}]}

    update = hook(state, {"configurable": {"thread_id": "thread-b"}})
    assert update == {"llm_input_messages": state["messages"]}
    from unimem.integrations.langgraph import turn_occasion_id

    assert calls == [("hello", turn_occasion_id(state, {"configurable": {"thread_id": "thread-b"}}), {})]

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


def test_pre_model_hook_reuses_context_during_tool_loop_and_separates_turns():
    from unimem.integrations.langgraph import turn_occasion_id

    calls = []

    class RecordingMemory:
        def recall(self, query, *, occasion_id, **kwargs):
            calls.append((query, occasion_id, kwargs))
            return [{"memory": f"Memory for {query}"}]

    hook = create_pre_model_hook(RecordingMemory())
    config = {"configurable": {"thread_id": "conversation"}}
    first = {"role": "user", "id": "turn-1", "content": "hello"}
    state = {"messages": [first]}
    initial = hook(state, config)
    loop_state = {"messages": [first, {"role": "assistant", "content": "calling a tool"},
                               {"role": "tool", "content": "tool output"}]}
    repeated = hook(loop_state, config)
    assert len(calls) == 1
    assert _message_content(initial["llm_input_messages"][0]) == _message_content(repeated["llm_input_messages"][0])
    assert calls[0][1] == turn_occasion_id(state, config)
    # Identical text in a new user message is a distinct causal occasion.
    next_state = {"messages": [*loop_state["messages"], {**first, "id": "turn-2"}]}
    hook(next_state, config)
    assert len(calls) == 2
    assert calls[0][1] != calls[1][1]


def test_pre_model_hook_scope_partitions_turn_cache_and_causal_identity():
    calls = []

    class RecordingMemory:
        def recall(self, query, *, occasion_id, **kwargs):
            calls.append((occasion_id, kwargs))
            return [{"memory": kwargs["tenant"]}]

    hook = create_pre_model_hook(RecordingMemory(), recall_kwargs=lambda config: {"tenant": config["configurable"]["tenant"]})
    state = {"messages": [{"role": "user", "id": "same-turn", "content": "hello"}]}
    for tenant in ("alice", "bob", "alice"):
        result = hook(state, {"configurable": {"thread_id": "same-thread", "tenant": tenant}})
        assert tenant in _message_content(result["llm_input_messages"][0])
    assert len(calls) == 2
    assert calls[0][0] != calls[1][0]


def test_pre_model_hook_outcome_joins_turn_assignment(tmp_path):
    from unimem.integrations.langgraph import record_outcome, turn_occasion_id

    holdout_io.configure(str(tmp_path), rate=0.0)
    memory = CausalMemory(lambda query, **kwargs: [{"id": "lesson", "memory": "test memory"}], root=str(tmp_path))
    hook = create_pre_model_hook(memory)
    config = {"configurable": {"thread_id": "conversation"}}
    state = {"messages": [{"role": "user", "id": "turn", "content": "hello"}]}
    hook(state, config)
    assert record_outcome(memory, config, state=state, succeeded=True)
    assert holdout_io.read_outcomes(str(tmp_path)) == {turn_occasion_id(state, config): True}


def test_pre_model_hook_cache_is_bounded_and_no_id_fallback_survives_tool_loop():
    calls = []

    class RecordingMemory:
        def recall(self, query, *, occasion_id, **kwargs):
            calls.append(occasion_id)
            return []

    hook = create_pre_model_hook(RecordingMemory(), cache_size=1)
    config = {"configurable": {"thread_id": "conversation"}}
    first = {"role": "user", "content": "hello"}
    hook({"messages": [first]}, config)
    loop = {"messages": [first, {"role": "tool", "content": "result"}]}
    hook(loop, config)
    assert len(calls) == 1
    hook({"messages": [*loop["messages"], {"role": "user", "content": "hello"}]}, config)
    assert len(calls) == 2
    assert calls[0] != calls[1]
    # Eviction may re-retrieve, while the causal identity remains deterministic.
    hook(loop, config)
    assert calls == [calls[0], calls[1], calls[0]]
