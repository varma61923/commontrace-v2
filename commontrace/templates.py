"""Scaffolding content generators for `commontrace init` / `capture` / `lesson new`."""
from __future__ import annotations

import datetime
from typing import Any

import yaml

from commontrace.paths import STARTER_DOMAINS


def lesson_frontmatter(
    slug: str,
    description: str,
    agent_type: str,
    domain: str,
    tags: list[str],
    applies_when: str,
    do_not_apply_when: str,
    importance: int = 3,
    importance_rationale: str = "",
    source_traces: list[str] | None = None,
    status: str = "active",
) -> dict[str, Any]:
    return {
        "name": slug,
        "description": description,
        "tags": tags,
        "agent_type": agent_type,
        "domain": domain,
        "importance": importance,
        "importance_rationale": importance_rationale or "needs calibration",
        "importance_history": [],
        "applies_when": applies_when or "TODO: precise activation condition",
        "do_not_apply_when": do_not_apply_when or "TODO: explicit counter-condition",
        "uses": 0,
        "last_hit": "NEVER",
        "source_traces": source_traces or [],
        "source_episodes": [],
        "hub_trace_id": None,
        "status": status,
    }


def lesson_body() -> str:
    return (
        "## Rule\n[1 actionable sentence]\n\n"
        "## Why\n[Factual observation or source incident, grounded in reality]\n\n"
        "## How to apply\n[When to invoke it, how to use it concretely]\n\n"
        "## Counter-examples\n[Cases where the rule does NOT apply]\n"
    )


def trace_frontmatter(
    trace_id: str,
    title: str,
    agent_type: str,
    tags: list[str],
    profile: str = "",
    outcome: dict[str, Any] | None = None,
    agent_id: str = "",
) -> dict[str, Any]:
    fm = {
        "id": trace_id,
        "title": title,
        "agent_type": agent_type,
        "agent_id": agent_id,
        "tags": tags,
        "profile": profile,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "watch_condition": "",
        "review_after": "",
        "supersedes_trace_id": "",
    }
    if outcome:
        fm["outcome"] = outcome
    return fm


def trace_body(context_text: str, solution_text: str) -> str:
    return f"## Context\n{context_text}\n\n## Solution\n{solution_text}\n"


def index_md(agent_type: str) -> str:
    domains = STARTER_DOMAINS.get(agent_type, STARTER_DOMAINS["custom"])
    # "code" scaffolds memory/episodes/ alongside memory/traces/ (init_cmd.py),
    # and code's own STARTER_DOMAINS entries are the only ones with episodes
    # to index -- everything below the "### <Domain>" heading was previously
    # unconditionally "#### Traces", so a code-type store's own index told
    # the reader to look in a directory ("Traces") that isn't where its
    # capture output actually lives.
    second = "Episodes" if agent_type == "code" else "Traces"
    sections = "\n\n".join(
        f"### {d.replace('-', ' ').title()}\n\n#### Lessons\n\n#### {second}\n" for d in domains
    )
    return (
        f"# Memory Index — agent_type: {agent_type}\n\n"
        "Hierarchical index by domain. See protocol/PROTOCOL.md for the full spec.\n\n"
        "## Usage Conventions\n\n"
        # "### <Domain>", matching what is actually generated below -- the
        # previous "## <Domain>" told a reader who followed the instruction
        # to add a domain at the wrong heading level, one above every
        # existing domain section.
        "- Add a `### <Domain>` section here when introducing a new domain.\n"
        "- Keep this file in sync with memory/lessons/ and memory/traces/.\n\n"
        "## Sections by Domain\n\n"
        f"{sections}\n"
    )


def dump_frontmatter(fm: dict[str, Any]) -> str:
    return yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
