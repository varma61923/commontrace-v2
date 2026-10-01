"""OpenAI Agents SDK: a memory tool keyed on the caller's task id.

    from commontrace.integrations.openai_agents import memory_tool

    agent = Agent(..., tools=[memory_tool(memory)])
    await Runner.run(agent, task, context={"occasion_id": task_id})
    memory.record_outcome(task_id, succeeded=passed)

The id is read from the run context (`context["occasion_id"]` or
`context.occasion_id`), which every tool receives. The trace's `group_id`
is only a fallback: with tracing disabled (OPENAI_AGENTS_DISABLE_TRACING)
the trace is a no-op with no group_id, even when RunConfig sets one. With
no id at all the tool returns nothing rather than invent an id no outcome
could be joined to. Verified against openai-agents 0.22 with a scripted
Model. No `from __future__ import annotations`: the SDK reads the
RunContextWrapper annotation to know to pass the context.
"""
from typing import Any

from commontrace.measure import default_text


def occasion_id(ctx: Any) -> str:
    context = getattr(ctx, "context", None)
    value = context.get("occasion_id") if isinstance(context, dict) else getattr(context, "occasion_id", None)
    if not value:
        from agents import get_current_trace

        value = getattr(get_current_trace(), "group_id", None)
    if not value:
        raise ValueError("no occasion id -- pass context={'occasion_id': <task id>} to Runner.run")
    return str(value)


def memory_tool(memory: Any, *, name: str = "recall_memory"):
    """A function tool the agent calls to retrieve memory, with the holdout
    applied. `memory` is a CausalMemory or a memory_adapters.MeasuredMemory."""
    from agents import RunContextWrapper, function_tool

    def recall_memory(ctx: RunContextWrapper[Any], query: str) -> list[str]:
        """Look up what this fleet has learned that applies to the task."""
        try:
            items = memory.recall(query, occasion_id=occasion_id(ctx))
        except Exception:  # noqa: BLE001 - a memory outage must not fail the run
            return []
        return [default_text(item) or str(item) for item in items]

    return function_tool(recall_memory, name_override=name)
