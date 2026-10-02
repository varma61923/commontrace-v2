"""Autonomous agent execution loop for CommonTrace.

Implements a Letta-style multi-turn agent loop that:
- Dynamically assembles prompt context from working memory blocks, active facts,
  and the knowledge graph profile
- Executes tool-use turns up to max_turns
- Logs each turn as an episodic trace with YAML frontmatter
- Triggers dynamic dreaming consolidation at configurable intervals

This enables agents to maintain stateful, governed memory across long-running tasks
while keeping every memory change cryptographically audited.
"""
from __future__ import annotations

import datetime
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class AgentTurn:
    """A single turn in the agent execution loop."""
    turn_index: int
    role: str  # "user" | "assistant" | "tool"
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.datetime.now(
        datetime.timezone.utc).isoformat())


@dataclass
class AgentRunResult:
    """Result of a completed agent run."""
    run_id: str
    prompt: str
    turns: list[AgentTurn]
    final_answer: str
    success: bool
    trace_path: str | None = None
    blocks_updated: int = 0
    facts_recorded: int = 0


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def _assemble_context(root: str, task_prompt: str) -> str:
    """Assemble a rich context string from working memory blocks + profile."""
    from commontrace import hierarchical, memory_blocks

    lines = [f"# Task\n{task_prompt}\n"]

    # Include active working memory blocks
    blocks = memory_blocks.list_blocks(root)
    if blocks:
        lines.append("# Working Memory\n")
        for b in blocks:
            lines.append(f"## {b.name} ({b.char_count}/{b.max_chars} chars)")
            lines.append(b.content)
            lines.append("")

    # Include high-confidence active facts
    facts = hierarchical.list_facts(root, status="active")[:10]
    if facts:
        lines.append("# Key Facts\n")
        for f in facts:
            scope_str = f" [{','.join(f.scopes)}]" if f.scopes else ""
            lines.append(f"- **{f.statement}** (conf: {f.confidence:.2f}){scope_str}")
        lines.append("")

    # Include discoverable skills as name + description only;
    # full SKILL.md bodies load on demand via commontrace.skills.load_body.
    try:
        from commontrace import skills as _skills

        discovered = _skills.discover(root)
        if discovered:
            lines.append(_skills.format_for_context(discovered))
    except Exception:
        pass

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Execution trace logging
# ---------------------------------------------------------------------------

def _write_execution_trace(
    root: str,
    run_id: str,
    prompt: str,
    turns: list[AgentTurn],
    final_answer: str,
    success: bool,
    agent_type: str = "agent",
) -> str:
    """Write a YAML-frontmatter trace to memory/traces/."""
    from commontrace import frontmatter, paths

    trace_dir = paths.traces_dir(root)
    os.makedirs(trace_dir, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    slug_title = prompt[:40].lower().replace(" ", "-").replace("/", "-")
    slug_title = "".join(c for c in slug_title if c.isalnum() or c == "-")
    filename = f"{date_str}_agent-run-{run_id[:8]}.md"
    trace_path = os.path.join(trace_dir, filename)

    summary_lines = [f"Agent run {run_id}", "", f"**Prompt:** {prompt}", ""]
    for turn in turns[-5:]:  # Last 5 turns for conciseness
        summary_lines.append(f"**Turn {turn.turn_index} [{turn.role}]:** {turn.content[:200]}")

    summary_lines += ["", f"**Final Answer:** {final_answer[:500]}"]

    frontmatter.write(trace_path, {
        "title": f"[Agent] {prompt[:80]}",
        "agent_type": agent_type,
        "run_id": run_id,
        "turns": len(turns),
        "success": success,
        "tags": ["agent-run"],
        "created_at": now.isoformat(),
    }, "\n".join(summary_lines))

    return trace_path


# ---------------------------------------------------------------------------
# The Agent Loop
# ---------------------------------------------------------------------------

class AgentLoop:
    """Letta-pattern multi-turn autonomous agent execution loop.

    Assembles bounded working memory context, executes tool-use turns up to
    max_turns, writes execution traces to memory/traces/, and optionally
    triggers dynamic dreaming consolidation.

    Usage::

        loop = AgentLoop(root="/path/to/store")
        result = loop.run(
            prompt="Fix the failing payment webhook",
            tool_executor=my_tool_fn,
        )
    """

    def __init__(
        self,
        root: str,
        agent_type: str = "agent",
        dream_every: int = 10,
    ) -> None:
        self.root = root
        self.agent_type = agent_type
        self.dream_every = dream_every  # trigger dreaming pass every N runs
        self._run_count = 0

    def run(
        self,
        prompt: str,
        tool_executor: Callable[[str, list[dict]], tuple[str, list[dict]]] | None = None,
        max_turns: int = 20,
        dream_on_complete: bool = False,
    ) -> AgentRunResult:
        """Execute a multi-turn agent loop for the given prompt.

        Args:
            prompt: The task prompt for the agent.
            tool_executor: Optional callable(context, tool_calls) → (response, results).
                If None, a no-op executor is used (useful for testing).
            max_turns: Maximum number of agent turns before stopping.
            dream_on_complete: If True, trigger a dreaming pass after the run.

        Returns:
            AgentRunResult with all turns, trace path, and outcome.
        """
        from commontrace import hierarchical, memory_blocks

        run_id = str(uuid.uuid4())
        turns: list[AgentTurn] = []
        blocks_updated = 0
        facts_recorded = 0

        context = _assemble_context(self.root, prompt)
        final_answer = ""
        success = False

        if tool_executor is None:
            # Default no-op executor: simulate a single completion turn
            tool_executor = _noop_executor

        for turn_idx in range(max_turns):
            # Execute one agent step
            try:
                response, tool_results = tool_executor(context, [])
            except Exception as exc:
                turns.append(AgentTurn(
                    turn_index=turn_idx,
                    role="error",
                    content=f"Executor error: {exc}",
                ))
                break

            turn = AgentTurn(
                turn_index=turn_idx,
                role="assistant",
                content=response,
                tool_results=tool_results,
            )
            turns.append(turn)

            # Process any memory block updates from tool results
            for result in tool_results:
                if result.get("type") == "memory_block_update":
                    name = result.get("name", "")
                    content = result.get("content", "")
                    mode = result.get("mode", "set")
                    if name and content:
                        try:
                            if mode == "append":
                                memory_blocks.append_block(self.root, name, content, actor=f"agent:{run_id[:8]}")
                            else:
                                memory_blocks.set_block(self.root, name, content, actor=f"agent:{run_id[:8]}")
                            blocks_updated += 1
                        except Exception:
                            pass

                elif result.get("type") == "fact_record":
                    stmt = result.get("statement", "")
                    if stmt:
                        try:
                            hierarchical.add_fact(
                                self.root,
                                statement=stmt,
                                category=result.get("category", "general"),
                                confidence=float(result.get("confidence", 0.8)),
                            )
                            facts_recorded += 1
                        except Exception:
                            pass

            # Check for terminal response
            if result.get("done", False) if tool_results else False:
                final_answer = response
                success = True
                break

            # After the last turn, set the final answer
            if turn_idx == max_turns - 1:
                final_answer = response
                success = True  # completed within turn budget

            # Update context for next turn
            context = _assemble_context(self.root, prompt)

        # Write execution trace
        trace_path = _write_execution_trace(
            self.root, run_id, prompt, turns, final_answer, success,
            agent_type=self.agent_type,
        )

        self._run_count += 1

        # Optionally trigger dreaming consolidation
        if dream_on_complete or (self.dream_every > 0 and self._run_count % self.dream_every == 0):
            try:
                _trigger_dream(self.root)
            except Exception:
                pass

        return AgentRunResult(
            run_id=run_id,
            prompt=prompt,
            turns=turns,
            final_answer=final_answer,
            success=success,
            trace_path=trace_path,
            blocks_updated=blocks_updated,
            facts_recorded=facts_recorded,
        )


def _noop_executor(
    context: str,
    tool_calls: list[dict],
) -> tuple[str, list[dict]]:
    """Default no-op executor that signals completion immediately."""
    return (
        f"[AgentLoop] Completed. Context length: {len(context)} chars.",
        [{"type": "done", "done": True}],
    )


def _trigger_dream(root: str) -> None:
    """Trigger a lightweight dreaming consolidation pass."""
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from commontrace.commands import dream_cmd

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        try:
            dream_cmd.main_dream(root, draft=False)
        except Exception:
            pass
