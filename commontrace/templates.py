"""Scaffolding content generators for `commontrace init` / `capture` / `lesson new`."""
from __future__ import annotations

import datetime
import re
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
    scopes: list[str] | None = None,
    valid_from: str = "",
    valid_until: str = "",
) -> dict[str, Any]:
    lesson = {
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
    if scopes:
        lesson["scopes"] = list(dict.fromkeys(str(scope).strip() for scope in scopes if str(scope).strip()))
    if valid_from:
        lesson["valid_from"] = valid_from
    if valid_until:
        lesson["valid_until"] = valid_until
    return lesson


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
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "watch_condition": "",
        "review_after": "",
        "supersedes_trace_id": "",
    }
    if outcome:
        fm["outcome"] = outcome
    return fm


PLACEHOLDER_MARKER = "TODO:"

BODY_KEY = "_body"

PLACEHOLDER_FIELDS = ("applies_when", "do_not_apply_when", "description")

PLACEHOLDER_SECTIONS = ("Rule", "Why", "How to apply", "Counter-examples")

_PLACEHOLDER_LINE_RE = re.compile(
    r"^\s*(?:" + re.escape(PLACEHOLDER_MARKER) + r"|\[[^\][]*\]\s*$)",
    re.IGNORECASE | re.MULTILINE,
)


def unfilled_placeholders(fm: dict[str, Any], body: str = "") -> list[str]:
    """Which parts of this lesson are still unedited scaffolding."""
    found: list[str] = []
    for field_name in PLACEHOLDER_FIELDS:
        value = fm.get(field_name)
        if isinstance(value, str) and _PLACEHOLDER_LINE_RE.search(value):
            found.append(field_name)
    for section in PLACEHOLDER_SECTIONS:
        match = re.search(
            rf"^##\s*{re.escape(section)}\s*\n(.*?)(?=\n##\s|\Z)",
            body,
            re.DOTALL | re.MULTILINE | re.IGNORECASE,
        )
        if match and _PLACEHOLDER_LINE_RE.search(match.group(1)):
            found.append(f"## {section}")
    return found


def trace_body(context_text: str, solution_text: str) -> str:
    return f"## Context\n{context_text}\n\n## Solution\n{solution_text}\n"


def index_md(agent_type: str, has_episodes: bool = False) -> str:
    domains = STARTER_DOMAINS.get(agent_type, STARTER_DOMAINS["custom"])
    second = "Episodes" if has_episodes else "Traces"
    sections = "\n\n".join(
        f"### {d.replace('-', ' ').title()}\n\n#### Lessons\n\n#### {second}\n" for d in domains
    )
    return (
        f"# Memory Index — agent_type: {agent_type}\n\n"
        "Hierarchical index by domain. See protocol/PROTOCOL.md for the full spec.\n\n"
        "## Usage Conventions\n\n"
        "- Add a `### <Domain>` section here when introducing a new domain.\n"
        "- Keep this file in sync with memory/lessons/ and memory/traces/.\n\n"
        "## Sections by Domain\n\n"
        f"{sections}\n"
    )


def dump_frontmatter(fm: dict[str, Any]) -> str:
    return yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
