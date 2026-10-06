"""Procedural memory architecture for recording and replaying structured agent workflows.

Adapted from Mem0 procedural memory and Letta episodic action logs:
- Records structured agent execution history: task objective, progress status, and numbered steps.
- Every agent action and verbatim output preserved for exact continuation.
- Enforces token budgets and tokens-per-retrieval accounting to prevent benchmark gaming.
- Supports serialization, parsing, retrieval, and disk persistence in the CommonTrace store.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any


@dataclasses.dataclass
class ProceduralStep:
    """A single sequential step executed by an agent during a task."""

    step_number: int
    action: str
    result: str
    key_findings: str = ""
    navigation_history: str = ""
    errors: str = ""
    current_context: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProceduralStep:
        return cls(
            step_number=int(data.get("step_number", 1)),
            action=str(data.get("action", "")),
            result=str(data.get("result", "")),
            key_findings=str(data.get("key_findings", "")),
            navigation_history=str(data.get("navigation_history", "")),
            errors=str(data.get("errors", "")),
            current_context=str(data.get("current_context", "")),
        )


@dataclasses.dataclass
class ProceduralMemory:
    """Complete procedural memory recording an agent's workflow trajectory."""

    id: str
    task_objective: str
    progress_status: str
    steps: list[ProceduralStep] = dataclasses.field(default_factory=list)
    agent_id: str = ""
    created_at: str = dataclasses.field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = dataclasses.field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def token_count(self) -> int:
        """Rough token estimate: ~4 chars per token."""
        rendered = format_procedural_memory(self)
        return max(1, len(rendered) // 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_objective": self.task_objective,
            "progress_status": self.progress_status,
            "agent_id": self.agent_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "steps": [s.to_dict() for s in self.steps],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProceduralMemory:
        return cls(
            id=str(data.get("id", uuid.uuid4().hex[:16])),
            task_objective=str(data.get("task_objective", "")),
            progress_status=str(data.get("progress_status", "")),
            agent_id=str(data.get("agent_id", "")),
            created_at=str(data.get("created_at", datetime.now(timezone.utc).isoformat())),
            updated_at=str(data.get("updated_at", datetime.now(timezone.utc).isoformat())),
            steps=[ProceduralStep.from_dict(s) for s in data.get("steps", [])],
            metadata=dict(data.get("metadata", {})),
        )


def format_procedural_memory(
    memory: ProceduralMemory,
    *,
    token_budget: int | None = None,
) -> str:
    """Render procedural memory into a structured markdown prompt context.

    If token_budget is provided and exceeded, compresses older step outputs
    while maintaining chronological completeness and all recent steps.
    """
    lines = [
        "## Summary of the agent's execution history",
        f"**Task Objective**: {memory.task_objective}",
        f"**Progress Status**: {memory.progress_status}",
        "",
    ]

    for step in memory.steps:
        lines.append(f"{step.step_number}. **Agent Action**: {step.action}")
        lines.append("   **Action Result**:")
        lines.append(f'      "{step.result}"')
        if step.key_findings:
            lines.append(f"   **Key Findings**: {step.key_findings}")
        if step.navigation_history:
            lines.append(f"   **Navigation History**: {step.navigation_history}")
        if step.errors:
            lines.append(f"   **Errors & Challenges**: {step.errors}")
        if step.current_context:
            lines.append(f"   **Current Context**: {step.current_context}")
        lines.append("")

    rendered = "\n".join(lines)
    if token_budget is not None and (len(rendered) // 4) > token_budget:
        # Budget enforcement: truncate earlier action results if over budget
        budget_lines = [
            "## Summary of the agent's execution history (budget-constrained)",
            f"**Task Objective**: {memory.task_objective}",
            f"**Progress Status**: {memory.progress_status}",
            "",
        ]
        total_steps = len(memory.steps)
        for i, step in enumerate(memory.steps):
            is_recent = i >= total_steps - 3
            result_preview = step.result if is_recent else (step.result[:120] + "... [truncated for token budget]")
            budget_lines.append(f"{step.step_number}. **Agent Action**: {step.action}")
            budget_lines.append("   **Action Result**:")
            budget_lines.append(f'      "{result_preview}"')
            if step.current_context:
                budget_lines.append(f"   **Current Context**: {step.current_context}")
            budget_lines.append("")
        rendered = "\n".join(budget_lines)

    return rendered


def _procedural_dir(root: str) -> str:
    path = os.path.join(root, "procedural")
    os.makedirs(path, exist_ok=True)
    return path


_MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _memory_file(root: str, memory_id: str) -> str:
    clean_id = str(memory_id or "").strip()
    if not _MEMORY_ID_RE.fullmatch(clean_id):
        raise ValueError("procedural memory id must contain only letters, numbers, '_' or '-'")
    return os.path.join(_procedural_dir(root), f"{clean_id}.json")


def save_procedural_memory(root: str, memory: ProceduralMemory) -> str:
    """Persist a procedural memory object to store disk."""
    file_path = _memory_file(root, memory.id)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(memory.to_dict(), f, indent=2, ensure_ascii=False)
    return file_path


def load_procedural_memory(root: str, memory_id: str) -> ProceduralMemory | None:
    """Load a procedural memory by its ID from disk."""
    try:
        file_path = _memory_file(root, memory_id)
    except ValueError:
        return None
    if not os.path.isfile(file_path):
        return None
    with open(file_path, encoding="utf-8") as f:
        data = json.load(f)
    return ProceduralMemory.from_dict(data)


def list_procedural_memories(root: str, *, agent_id: str | None = None) -> list[dict[str, Any]]:
    """List stored procedural memories with overview summaries and token counts."""
    folder = _procedural_dir(root)
    items = []
    for fname in os.listdir(folder):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, fname), encoding="utf-8") as f:
                data = json.load(f)
            if agent_id and data.get("agent_id") != agent_id:
                continue
            mem = ProceduralMemory.from_dict(data)
            items.append({
                "id": mem.id,
                "task_objective": mem.task_objective,
                "progress_status": mem.progress_status,
                "agent_id": mem.agent_id,
                "steps_count": len(mem.steps),
                "token_estimate": mem.token_count,
                "updated_at": mem.updated_at,
            })
        except Exception:
            continue
    items.sort(key=lambda x: str(x.get("updated_at", "")), reverse=True)
    return items
