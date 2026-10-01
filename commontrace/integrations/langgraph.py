"""LangGraph middleware: derive the occasion id `CausalMemory` needs from
LangGraph's own per-thread config, instead of a caller inventing one.

`commontrace/measure.py`'s `CausalMemory` already wraps ANY memory's
retrieval call with a randomized holdout and joins it to a reported
outcome. The only framework-specific piece a LangGraph integration needs to
add is where the occasion id comes from: LangGraph already gives every
invocation one -- `config["configurable"]["thread_id"]`, threaded through
by every checkpointer -- so a lesson withheld for thread "conv-42" stays
withheld across every node call in that same run, and the causal
comparison is keyed on the unit LangGraph itself already calls one
conversation/run, not re-derived or invented here.

This module adds two thin functions on top of an ALREADY-CONSTRUCTED
`CausalMemory` -- it does not build its own retrieval, ranking, or holdout
logic, on purpose: that would be a second implementation of exactly what
`measure.py` exists to be the one implementation of.

    from commontrace.measure import CausalMemory
    from commontrace.integrations.langgraph import with_lessons, record_outcome

    memory = CausalMemory(my_store.search)
    graph.add_node("respond", with_lessons(respond_node, memory))
    ...
    result = graph.invoke(state, config={"configurable": {"thread_id": "conv-42"}})
    record_outcome(memory, config, succeeded=result["resolved"])

Requires nothing beyond `commontrace` itself to IMPORT -- this module does
not import `langgraph` at all, since all it touches is the plain `dict`
shape LangGraph passes as `config`. A project not using LangGraph pays
nothing for this file's existence; a project using it needs `langgraph`
installed regardless, as an application dependency this module does not
duplicate.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from commontrace.measure import CausalMemory

#: The state key `with_lessons` writes injected items to, by default.
STATE_KEY = "commontrace_lessons"


def thread_id(config: dict | None) -> str:
    """LangGraph's own per-invocation identifier.

    Raises ValueError rather than inventing one: a made-up id would be
    unreachable by `record_outcome`, which reads the SAME config for the
    SAME key on a later call -- a fabricated fallback here would silently
    orphan every assignment logged against it, eligible forever, joined to
    an outcome never.
    """
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
    """Wrap a LangGraph node so it runs with `memory`'s items injected under
    `memory`'s configured holdout, keyed on this thread's own id.

    `node` must accept `(state, config)` -- LangGraph inspects a node's own
    signature to decide whether to pass `config` at all, and this wrapper's
    signature always declares both so it always receives one to read the
    thread id from, regardless of whether the WRAPPED node cares about it.

    A failure here (no thread_id, an unreachable memory, a malformed item)
    degrades to an empty injection rather than failing the node -- the same
    "never write to / crash the caller" stance `CausalMemory` already takes
    toward the store it wraps, extended to the graph calling it.
    """
    # `config` is deliberately UNANNOTATED, not `dict | None`: LangGraph
    # inspects a registered node's own signature to decide whether its
    # second parameter is the special runtime config LangChain forwards,
    # and it only recognizes that by the parameter name `config` (or a
    # `RunnableConfig` type annotation this module deliberately does not
    # import -- see the module docstring on why it never imports langgraph/
    # langchain at all). Annotating it `dict | None` was tried and
    # reproduced live: LangChain's own runnable introspection then decides
    # this is NOT the config parameter and silently passes `None` instead
    # of the real one, so `thread_id(None)` always raised and every
    # injection silently degraded to empty -- exactly the failure mode this
    # function's own try/except is supposed to be for genuine failures, not
    # for a parameter that was never actually reached.
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
    """Report this thread's outcome via the same occasion id `with_lessons`
    used. Returns False (never raises) when no thread_id is present -- the
    same degrade-don't-crash contract `with_lessons` follows."""
    try:
        occasion_id = thread_id(config)
    except ValueError:
        return False
    return memory.record_outcome(occasion_id, succeeded=succeeded)
