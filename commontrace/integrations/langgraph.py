from __future__ import annotations

from collections.abc import Callable
from typing import Any

from commontrace.measure import CausalMemory

STATE_KEY = "commontrace_lessons"


def thread_id(config: dict | None) -> str:
    """LangGraph's own per-invocation identifier."""
    value = ((config or {}).get("configurable") or {}).get("thread_id")
    if not value or not isinstance(value, str):
        raise ValueError(
            "no config['configurable']['thread_id'] found -- pass a config carrying "
            "one, e.g. graph.invoke(state, config={'configurable': {'thread_id': 'conv-42'}})."
        )
    return value


def with_lessons(
    node: Callable[[dict, dict | None], dict],
    memory: CausalMemory,
    *,
    state_query: Callable[[dict], Any] = lambda state: state.get("input", ""),
    state_key: str = STATE_KEY,
) -> Callable[[dict, dict | None], dict]:
    def wrapped(state, config=None):
        items: list[Any] = []
        try:
            occasion_id = thread_id(config)
            items = memory.recall(state_query(state), occasion_id=occasion_id)
        except Exception:  # noqa: BLE001 - see docstring: never fail the graph
            items = []
        return node({**state, state_key: items}, config)

    return wrapped


def record_outcome(memory: CausalMemory, config: dict | None, *, succeeded: bool) -> bool:
    try:
        occasion_id = thread_id(config)
    except ValueError:
        return False
    return memory.record_outcome(occasion_id, succeeded=succeeded)
