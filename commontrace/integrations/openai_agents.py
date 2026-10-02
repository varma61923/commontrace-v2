"""OpenAI Agents SDK: a memory tool keyed on the caller's task id."""
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
    from agents import RunContextWrapper, function_tool

    def recall_memory(ctx: RunContextWrapper[Any], query: str) -> list[str]:
        """Look up what this fleet has learned that applies to the task."""
        try:
            items = memory.recall(query, occasion_id=occasion_id(ctx))
        except Exception:  # noqa: BLE001 - a memory outage must not fail the run
            return []
        return [default_text(item) or str(item) for item in items]

    return function_tool(recall_memory, name_override=name)
