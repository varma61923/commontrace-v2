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
        # UTC, not naive local time: a fleet spans machines/regions, and a
        # timestamp with no offset is ambiguous across them in a way that
        # defeats any later cross-agent chronological comparison.
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "watch_condition": "",
        "review_after": "",
        "supersedes_trace_id": "",
    }
    if outcome:
        fm["outcome"] = outcome
    return fm


# --- Unfilled-placeholder detection ----------------------------------------
#
# Every placeholder this module and `commontrace distill` write starts with
# this marker. Detecting them is not cosmetic: an all-placeholder lesson was
# a fully valid, fully active, fully shippable lesson end to end.
#
# Reproduced on a real store, start to finish: `commontrace distill` writes a
# candidate whose Rule, How-to-apply, Counter-examples, applies_when and
# do_not_apply_when are all "TODO: ..."; `lesson approve` activates it;
# `lesson validate` reports "1/1 lessons valid"; `query` returns it as the
# top hit; `taxonomy` reports the failure pattern it came from as "covered
# by" it; `pilot` reports "Gaps: 0"; and `sync --push` uploads it to the Hub,
# where `search_traces` serves it to the whole fleet.
#
# So the pipeline's own scaffolding could be promoted to a fleet-wide
# instruction with nothing in it, and every report the customer reads would
# describe that as coverage. This module's other comments call that failure
# by its name in a different context: "for an agent, which injects whatever
# it is given, it is context poisoning with this product's name on it."
PLACEHOLDER_MARKER = "TODO:"

# Key under which evidence_io.load_active_lessons stashes a lesson's Markdown
# body on the frontmatter dict it returns, so a caller holding only that dict
# can still run the whole-lesson check below. Underscore-prefixed so it can
# never collide with a real frontmatter field.
BODY_KEY = "_body"

# Frontmatter fields a placeholder can legitimately be written into by the
# templates below, and which a human is expected to replace before the
# lesson is activated.
PLACEHOLDER_FIELDS = ("applies_when", "do_not_apply_when", "description")

# Body sections the lesson template scaffolds.
PLACEHOLDER_SECTIONS = ("Rule", "Why", "How to apply", "Counter-examples")

# The two scaffold styles this project actually writes, both of which mark a
# spot a human is meant to replace:
#   - "TODO: ..."  -- `commontrace distill`'s auto-proposed candidates
#   - "[1 actionable sentence]" -- `lesson new`/`lesson_body`'s template
# Both had to be covered, not just the first: `lesson new` followed straight
# by `lesson approve` activates a lesson whose entire rule is still
# "[1 actionable sentence]", which is the same defect by a different marker.
#
# The bracket form requires the line to be ONLY the bracketed text, so real
# prose that happens to contain brackets ("prefer `arr[0]` over ...") is not
# mistaken for scaffolding.
_PLACEHOLDER_LINE_RE = re.compile(
    r"^\s*(?:" + re.escape(PLACEHOLDER_MARKER) + r"|\[[^\][]*\]\s*$)",
    re.IGNORECASE | re.MULTILINE,
)


def unfilled_placeholders(fm: dict[str, Any], body: str = "") -> list[str]:
    """Which parts of this lesson are still unedited scaffolding.

    Returns human-readable names ("applies_when", "## Rule"), empty when the
    lesson has been genuinely written. A placeholder is recognised by the
    line STARTING with the marker, so a lesson that legitimately mentions
    "TODO:" inside real prose (a code-review lesson about TODO comments is
    an obvious one) is not caught by it.
    """
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
