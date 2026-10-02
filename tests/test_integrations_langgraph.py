from __future__ import annotations

import json

import pytest

langgraph = pytest.importorskip("langgraph.graph", reason="langgraph is an optional integration dependency")

from typing import TypedDict  # noqa: E402

from commontrace import holdout_io  # noqa: E402
from commontrace.integrations import langgraph as ct_langgraph  # noqa: E402
from commontrace.measure import CausalMemory  # noqa: E402

StateGraph = langgraph.StateGraph
END = langgraph.END


class _State(TypedDict, total=False):
    input: str
    commontrace_lessons: list
    resolved: bool


class FakeStore:
    def __init__(self, memories):
        self.memories = memories

    def search(self, query, **kwargs):
        return [dict(m) for m in self.memories]


def _configure(root, rate):
    holdout_io.configure(str(root), rate=rate)


def _build_graph(node):
    graph = StateGraph(_State)
    graph.add_node("n", node)
    graph.set_entry_point("n")
    graph.add_edge("n", END)
    return graph.compile()


class TestThreadId:
    def test_extracts_the_configurable_thread_id(self):
        config = {"configurable": {"thread_id": "conv-42"}}
        assert ct_langgraph.thread_id(config) == "conv-42"

    def test_raises_when_config_is_none(self):
        with pytest.raises(ValueError, match="thread_id"):
            ct_langgraph.thread_id(None)

    def test_raises_when_configurable_is_missing(self):
        with pytest.raises(ValueError, match="thread_id"):
            ct_langgraph.thread_id({"metadata": {}})

    def test_raises_when_thread_id_is_not_a_string(self):
        with pytest.raises(ValueError, match="thread_id"):
            ct_langgraph.thread_id({"configurable": {"thread_id": 42}})


class TestWithLessons:
    def test_injected_items_reach_the_wrapped_node_via_a_real_graph(self, tmp_path):
        _configure(tmp_path, 0.0)
        store = FakeStore([{"id": "m1", "memory": "set an idempotency key"}])
        memory = CausalMemory(store.search, root=str(tmp_path))

        def node(state, config=None):
            return {"resolved": bool(state.get("commontrace_lessons"))}

        wrapped = ct_langgraph.with_lessons(node, memory)
        compiled = _build_graph(wrapped)
        result = compiled.invoke(
            {"input": "webhook fired twice"},
            config={"configurable": {"thread_id": "conv-1"}},
        )
        assert result["resolved"] is True

    def test_the_occasion_id_used_is_the_graphs_own_thread_id(self, tmp_path):
        _configure(tmp_path, 0.5)
        store = FakeStore([{"id": "m1", "memory": "x"}])
        memory = CausalMemory(store.search, root=str(tmp_path))

        def node(state, config=None):
            return {"resolved": True}

        compiled = _build_graph(ct_langgraph.with_lessons(node, memory))
        compiled.invoke({"input": "q"}, config={"configurable": {"thread_id": "conv-99"}})

        log_path = holdout_io.holdout_log_path(str(tmp_path))
        with open(log_path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh]
        assert rows[0]["occasion_id"] == "conv-99"

    def test_a_missing_thread_id_degrades_to_no_lessons_not_a_crash(self, tmp_path):
        store = FakeStore([{"id": "m1", "memory": "x"}])
        memory = CausalMemory(store.search, root=str(tmp_path))
        seen = {}

        def node(state, config=None):
            seen["lessons"] = state.get("commontrace_lessons")
            return {"resolved": True}

        compiled = _build_graph(ct_langgraph.with_lessons(node, memory))
        compiled.invoke({"input": "q"})
        assert seen["lessons"] == []

    def test_a_custom_state_query_is_used(self, tmp_path):
        _configure(tmp_path, 0.0)
        captured_queries = []

        def search(query, **kwargs):
            captured_queries.append(query)
            return []

        memory = CausalMemory(search, root=str(tmp_path))

        def node(state, config=None):
            return {"resolved": True}

        wrapped = ct_langgraph.with_lessons(
            node, memory, state_query=lambda state: state["input"].upper(),
        )
        compiled = _build_graph(wrapped)
        compiled.invoke({"input": "hello"}, config={"configurable": {"thread_id": "conv-2"}})
        assert captured_queries == ["HELLO"]

    def test_a_broken_memory_degrades_to_no_lessons_not_a_crash(self, tmp_path):
        def boom(query, **kwargs):
            raise RuntimeError("store is down")

        memory = CausalMemory(boom, root=str(tmp_path))
        seen = {}

        def node(state, config=None):
            seen["lessons"] = state.get("commontrace_lessons")
            return {"resolved": True}

        compiled = _build_graph(ct_langgraph.with_lessons(node, memory))
        result = compiled.invoke(
            {"input": "q"}, config={"configurable": {"thread_id": "conv-3"}},
        )
        assert seen["lessons"] == []
        assert result["resolved"] is True


class TestRecordOutcome:
    def test_reports_the_same_occasion_id_with_lessons_used(self, tmp_path):
        _configure(tmp_path, 0.0)
        store = FakeStore([{"id": "m1", "memory": "x"}])
        memory = CausalMemory(store.search, root=str(tmp_path))
        config = {"configurable": {"thread_id": "conv-7"}}

        def node(state, config=None):
            return {"resolved": True}

        compiled = _build_graph(ct_langgraph.with_lessons(node, memory))
        compiled.invoke({"input": "q"}, config=config)

        assert ct_langgraph.record_outcome(memory, config, succeeded=True) is True
        assert holdout_io.read_outcomes(str(tmp_path)) == {"conv-7": True}

    def test_returns_false_rather_than_raising_with_no_thread_id(self, tmp_path):
        memory = CausalMemory(lambda q, **kw: [], root=str(tmp_path))
        assert ct_langgraph.record_outcome(memory, None, succeeded=True) is False
