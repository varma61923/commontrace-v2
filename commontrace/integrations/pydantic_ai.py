"""Pydantic AI: a memory tool keyed on the run's own `conversation_id`
(or `run_id`), so `CausalMemory`'s holdout and outcome join need no
caller-invented id.

    from commontrace.integrations.pydantic_ai import memory_tool, record_outcome

    agent = Agent(model, tools=[memory_tool(memory)])
    result = agent.run_sync("...")
    record_outcome(memory, result, succeeded=passed)

Verified against pydantic-ai 2.52 with its TestModel. No
`from __future__ import annotations` here: Pydantic AI reads the tool's
`RunContext` annotation as a real object to know to pass the context.
"""
from typing import Any

from commontrace.measure import default_text

OCCASION_FIELDS = ("conversation_id", "run_id")


def _occasion(source: Any, field: str) -> str:
    if field not in OCCASION_FIELDS:
        raise ValueError(f"occasion must be one of {OCCASION_FIELDS}")
    value = getattr(source, field, None)
    if not value:
        raise ValueError(f"no {field} on this run")
    return str(value)


def _text(item: Any) -> str:
    return default_text(item) or str(item)


def memory_tool(memory: Any, *, occasion: str = "conversation_id", name: str = "recall_memory"):
    """A tool the agent calls to retrieve memory, with the holdout applied.
    `memory` is a CausalMemory or a memory_adapters.MeasuredMemory."""
    from pydantic_ai import RunContext, Tool

    def recall_memory(ctx: RunContext[Any], query: str) -> list[str]:
        """Look up what this fleet has learned that applies to the task."""
        try:
            items = memory.recall(query, occasion_id=_occasion(ctx, occasion))
        except Exception:  # noqa: BLE001 - a memory outage must not fail the run
            return []
        return [_text(item) for item in items]

    return Tool(recall_memory, name=name, takes_ctx=True)


def record_outcome(memory: Any, result: Any, *, succeeded: bool, occasion: str = "conversation_id") -> bool:
    """Report the run's outcome under the same id the tool used."""
    return memory.record_outcome(_occasion(result, occasion), succeeded=succeeded)
