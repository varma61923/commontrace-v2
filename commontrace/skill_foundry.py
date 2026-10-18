"""UniMem bounded skill induction and verifier tasks, with no model dependency.

Agent Workflow Memory (arXiv:2409.07429), Inducing Programmatic Skills for
Agentic Tasks (arXiv:2504.06821) and SkillsBench (arXiv:2602.12670) motivate the
interfaces. Deterministic templates and lexical verifier tasks are reference
heuristics, not claims to reproduce those results. A sandbox runner supplied by
the operator must enforce process isolation for programmatic candidates.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Protocol

from commontrace import frontier_protocol as fp

AGENT_SKILLS_VERSION = "69ef37e9424c0a7ea9dd2293b559e43ec8176379"


class Inducer(Protocol):
    def __call__(self, trajectories: Sequence[dict[str, Any]], *, name: str) -> dict[str, Any]: ...


class Sandbox(Protocol):
    def __call__(self, program: str, task: dict[str, Any]) -> str: ...


def metadata(name: str, description: str) -> dict[str, str]:
    if (not isinstance(name, str) or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None
            or len(name) > 64):
        raise ValueError("UniMem Agent Skills names require a lowercase, single-hyphen-separated slug")
    fp.text(description, "Agent Skills description", 1024)
    return {"name": name, "description": description}


def template(trajectories: Sequence[dict[str, Any]], *, name: str) -> dict[str, Any]:
    fp.count(len(trajectories), 1, 32)
    steps = []
    for trace in trajectories:
        # Source bytes are quoted data; this template never executes their text.
        solution = fp.text(trace.get("solution_text", ""), "measured successful procedure", 12000)
        if solution not in steps:
            steps.append(solution)
    return {"format": "prose", "body": "\n\n".join([
        "# " + name, "## Activation", "Confirm the task matches the measured source scope.",
        "## Procedure", *[f"{i+1}. {step}" for i, step in enumerate(steps)],
        "## Verification", "Verify the outcome independently. Stop on an unmet precondition."])}


def candidate(trajectories: Sequence[dict[str, Any]], *, name: str, description: str,
              inducer: Inducer | None = None) -> dict[str, Any]:
    fields = metadata(name, description)
    value = (inducer or template)(trajectories, name=name)
    fp.wire(value)
    if set(value) != {"format", "body"} or value["format"] not in ("prose", "program"):
        raise ValueError("UniMem inducer must return format and body only")
    body = fp.text(value["body"], "candidate skill body", 48000)
    return {**fields, "body": body, "format": value["format"], "content_hash": fp.digest(body),
            "classification": "heuristic", "method": "template" if inducer is None else "operator-configured inducer"}


def prescreen(value: dict[str, Any], tasks: Sequence[dict[str, Any]], *,
              sandbox: Sandbox | None = None) -> dict[str, Any]:
    """Fail-closed reference verifier: lexical procedures or operator sandbox.

    A task's passing and failing examples must establish that its verifier can
    distinguish its fixture cases before the candidate is tested. Source failure
    provenance is checked by the I/O layer. Passing is eligibility, not efficacy.
    """
    fp.count(len(tasks), 1, 64)
    fp.wire(list(tasks))
    results = []
    for task in tasks:
        fp.text(task["trace_id"])
        fp.text(task["instruction"], "verifier instruction", 4096)
        token = fp.text(task["verifier"]["contains"], "required verified step", 256)
        def verify(response: str) -> bool:
            return token in response
        passing = fp.text(task["passing"], "passing verifier fixture", 12000)
        failing = fp.text(task["failing"], "failing verifier fixture", 12000)
        if not verify(passing) or verify(failing):
            raise ValueError("UniMem verifier fixtures do not distinguish passing and failing cases")
        if value["format"] == "program":
            if sandbox is None:
                raise ValueError("UniMem programmatic skills require an operator-configured sandbox runner")
            response = sandbox(value["body"], task)
            fp.text(response, "sandbox response", 48000)
        else:
            response = value["body"]
        passed = verify(response)
        results.append({"trace_id": task["trace_id"], "passed": passed,
                        "reason": "required step present" if passed else "required step absent"})
    return {"passed": all(r["passed"] for r in results), "tasks": results, "classification": "heuristic",
            "assumptions": ["operator verifiers reflect the failure cluster", "lexical checks do not prove semantics"]}


def exposed(value: dict[str, Any], injected: bool) -> list[dict[str, Any]]:
    """Resolve the delivered catalog from a canonical arm, without executing it."""
    if type(injected) is not bool:
        raise ValueError("UniMem skill delivery requires a binary canonical assignment")
    return [value] if injected else []


def document(value: dict[str, Any]) -> str:
    """Agent Skills body; the signed card binds this complete rendered string."""
    body = value["body"]
    if value["format"] == "program":
        body = ("# " + value["name"] + "\n\nRun this helper only in the configured sandbox.\n\n"
                + "\n".join("    " + line for line in body.splitlines()))
    # Match the managed folder reader's universal-newline and leading-newline
    # normalization before signing the rendered body commitment.
    body = str(body).replace("\r\n", "\n").replace("\r", "\n").lstrip("\n")
    return body + "\n\n## Evidence\nSee [signed evidence](EVIDENCE.json).\n"
