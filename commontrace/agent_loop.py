"""A memory-governed agent loop: retrieve, act, record, and consolidate."""
from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

MAX_CONTEXT_CHARS = 24_000
MAX_LESSONS = 5
MAX_FACTS = 10

Executor = Callable[[str, list[dict[str, Any]]], tuple[str, list[dict[str, Any]]]]


@dataclass
class AgentTurn:
    """One step of a run."""
    turn_index: int
    role: str
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())


@dataclass
class AgentRunResult:
    """The outcome of a run."""
    run_id: str
    prompt: str
    turns: list[AgentTurn]
    final_answer: str
    success: bool
    trace_path: str | None = None
    blocks_updated: int = 0
    facts_recorded: int = 0
    updates_refused: int = 0


def _clean(text: str) -> bool:
    from commontrace import memory_guard

    return not memory_guard.scan_injection(text or "")


def _relevant_lessons(root: str, task: str) -> list[tuple[str, str]]:
    from commontrace import frontmatter, injection_guard, lesson_cache, retrieval, retrieval_io

    try:
        lessons, term_cache = lesson_cache.load_active_with_terms(root, None, reader=frontmatter.read)
        lessons = lesson_cache.filter_eligible(lessons)
        config = retrieval_io.load_config(root)
        ranked = retrieval.rank_lessons(task, lessons, top_k=MAX_LESSONS, floor=config.floor,
                                        scorer=config.scorer, term_cache=term_cache)
    except (OSError, ValueError, frontmatter.FrontmatterError):
        return []
    items = []
    for ranked_lesson in ranked:
        try:
            fm, body = frontmatter.read(ranked_lesson.path)
        except (OSError, ValueError, frontmatter.FrontmatterError):
            continue
        if not lesson_cache.fresh_eligible(
            ranked_lesson.path, fm, slug=ranked_lesson.slug, body=body, root=root,
        ):
            continue
        items.append({"slug": ranked_lesson.slug, "description": str(fm.get("description", "")),
                      "applies_when": str(fm.get("applies_when", ""))})
    clean, _quarantined = injection_guard.screen(items)
    return [(i["slug"], i["description"]) for i in clean]


def _assemble_context(root: str, task_prompt: str) -> str:
    from commontrace import hierarchical, injection_guard, memory_blocks

    lines = [f"# Task\n{task_prompt}\n"]
    lessons = _relevant_lessons(root, task_prompt)
    if lessons:
        lines.append("# Relevant lessons\n" + injection_guard.NOTICE + "\n")
        lines.extend(f"- {slug}: {desc}" for slug, desc in lessons)
        lines.append("")
    blocks = [b for b in memory_blocks.list_blocks(root) if _clean(b.content)]
    if blocks:
        lines.append("# Working memory\n")
        for b in blocks:
            lines.extend([f"## {b.name} ({b.char_count}/{b.max_chars} chars)", b.content, ""])
    facts = [f for f in hierarchical.search_facts(root, task_prompt, limit=MAX_FACTS * 2)
             if _clean(f[0].statement)][:MAX_FACTS]
    if facts:
        lines.append("# Key facts\n")
        for fact, _score in facts:
            scope_str = f" [{','.join(fact.scopes)}]" if fact.scopes else ""
            lines.append(f"- {fact.statement} (conf: {fact.confidence:.2f}){scope_str}")
        lines.append("")
    try:
        from commontrace import skills

        discovered = skills.discover(root, include_bundled=False)
        if discovered:
            lines.append(skills.format_for_context(discovered))
    except (OSError, ValueError):
        pass
    return "\n".join(lines)[:MAX_CONTEXT_CHARS]


def _apply_update(root: str, run_id: str, update: dict[str, Any], result: AgentRunResult) -> None:
    from commontrace import hierarchical, memory_blocks, memory_guard

    kind = update.get("type")
    if kind not in ("memory_block_update", "fact_record"):
        return
    text = str(update.get("content") if kind == "memory_block_update" else update.get("statement") or "")
    if not text.strip():
        return
    if memory_guard.scan_fields({"text": text}).should_block:
        result.updates_refused += 1
        return
    actor = f"agent:{run_id[:8]}"
    try:
        if kind == "memory_block_update":
            name = str(update.get("name", ""))
            if update.get("mode") == "append":
                memory_blocks.append_block(root, name, text, actor=actor)
            else:
                memory_blocks.set_block(root, name, text, actor=actor)
            result.blocks_updated += 1
        else:
            hierarchical.add_fact(
                root, statement=text, category=str(update.get("category", "general")),
                confidence=float(update.get("confidence", 0.8)), source_trace_id=run_id,
            )
            result.facts_recorded += 1
    except (memory_blocks.MemoryBlockError, ValueError, TypeError, OSError):
        result.updates_refused += 1


def _write_execution_trace(root: str, result: AgentRunResult, agent_type: str) -> str | None:
    from commontrace import trace_io

    steps = "\n".join(f"- turn {t.turn_index} [{t.role}]: {t.content[:300]}" for t in result.turns[-10:])
    try:
        return trace_io.write_new(
            root,
            title=f"Agent run: {result.prompt[:120]}",
            context=f"{result.prompt}\n\nSteps:\n{steps}",
            solution=result.final_answer[:4000] or "No answer was produced.",
            tags=["agent-run"],
            agent_type=agent_type,
            trace_id=result.run_id,
            outcome={"resolved": bool(result.success)},
        )
    except ValueError:
        return None


class AgentLoop:
    """Run an executor against store memory for up to `max_turns` steps."""

    def __init__(self, root: str, agent_type: str = "agent", dream_every: int = 10) -> None:
        self.root = root
        self.agent_type = agent_type
        self.dream_every = dream_every
        self._run_count = 0

    def run(
        self,
        prompt: str,
        tool_executor: Executor | None = None,
        max_turns: int = 20,
        dream_on_complete: bool = False,
    ) -> AgentRunResult:
        executor = tool_executor or _noop_executor
        result = AgentRunResult(run_id=str(uuid.uuid4()), prompt=prompt, turns=[], final_answer="", success=False)
        history: list[dict[str, Any]] = []
        for turn_idx in range(max(1, int(max_turns))):
            context = _assemble_context(self.root, prompt)
            try:
                response, tool_results = executor(context, history)
            except Exception as exc:  # noqa: BLE001 - an executor failure ends the run, it does not crash it
                result.turns.append(AgentTurn(turn_index=turn_idx, role="error", content=f"Executor error: {exc}"))
                break
            tool_results = [r for r in (tool_results or []) if isinstance(r, dict)]
            result.turns.append(AgentTurn(turn_index=turn_idx, role="assistant", content=str(response),
                                          tool_results=tool_results))
            history.append({"turn": turn_idx, "response": str(response), "results": tool_results})
            for update in tool_results:
                _apply_update(self.root, result.run_id, update, result)
            result.final_answer = str(response)
            if any(r.get("done") for r in tool_results):
                result.success = True
                break
        result.trace_path = _write_execution_trace(self.root, result, self.agent_type)
        self._run_count += 1
        if dream_on_complete or (self.dream_every > 0 and self._run_count % self.dream_every == 0):
            _trigger_dream(self.root)
        return result


def _noop_executor(context: str, history: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    return f"[AgentLoop] Completed. Context length: {len(context)} chars.", [{"type": "done", "done": True}]


_LLM_INSTRUCTIONS = """You are an agent working on the task below, with memory from earlier runs.
Reply with ONE JSON object and nothing else:
{"response": "<what you did or concluded this step>",
 "done": <true when the task is complete>,
 "memory": [{"type": "fact_record", "statement": "<a durable fact worth remembering>"},
            {"type": "memory_block_update", "name": "<block>", "content": "<text>", "mode": "set|append"}]}
Only record memory that will help a future run; leave "memory" empty otherwise.
"""


def llm_executor(config=None) -> Executor:
    """An executor backed by the configured LLM provider (see `commontrace.llm`)."""
    from commontrace import llm

    cfg = config or llm.load_config()

    def run(context: str, history: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        previous = "\n".join(f"Step {h['turn']}: {h['response'][:1000]}" for h in history[-5:])
        prompt = f"{_LLM_INSTRUCTIONS}\n{context}\n" + (f"\n# Your previous steps\n{previous}\n" if previous else "")
        text, _usage = llm.complete(prompt, cfg)
        try:
            reply = llm._extract_json_object(text)
        except (ValueError, llm.LLMDraftRejected):
            return text.strip(), [{"type": "done", "done": True}]
        results = [m for m in reply.get("memory") or [] if isinstance(m, dict)]
        if reply.get("done"):
            results.append({"type": "done", "done": True})
        response = reply.get("response")
        return (response if isinstance(response, str) else json.dumps(response)), results

    return run


def _trigger_dream(root: str) -> None:
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from commontrace.commands import dream_cmd

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        try:
            dream_cmd.main_dream(root, draft=False)
        except Exception:  # noqa: BLE001 - consolidation is best-effort after a run
            pass
