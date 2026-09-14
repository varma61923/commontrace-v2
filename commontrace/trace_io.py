"""Read a Trace file as the full object protocol/schemas/trace.schema.json describes.

Trace files store `context_text` / `solution_text` as Markdown body sections
(## Context / ## Solution) for human readability, while every other field
lives in YAML frontmatter (see templates.trace_frontmatter). Schema
validation needs the merged view -- this is that seam.
"""
from __future__ import annotations

import re
from typing import Any

from commontrace import frontmatter as frontmatter_io

# IGNORECASE: a hand-written `## context` silently produced an empty
# context_text (and then a schema-invalid trace) because the pattern only
# matched the capitalized form. The files this protocol expects people to
# hand-edit should not depend on getting the shift key right.
_SECTION_RE = re.compile(
    r"^##\s*(Context|Solution)\s*\n(.*?)(?=\n##\s*(?:Context|Solution)\s*\n|\Z)",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)


def _first_wins(body: str) -> dict[str, str]:
    """Section name -> text, keeping the FIRST occurrence of each name.

    A dict comprehension over finditer keeps the LAST, so a heading that
    appears again later in the body -- most realistically inside a fenced
    code block quoting a trace, which this regex cannot see into --
    replaced the real section. Reproduced: a body whose Solution quoted an
    example `## Context` had its context replaced by the quoted fragment,
    trailing code fence included.
    """
    out: dict[str, str] = {}
    for m in _SECTION_RE.finditer(body):
        key = m.group(1).lower()
        if key not in out:
            out[key] = m.group(2).strip()
    return out


def read(path: str) -> tuple[dict[str, Any], str]:
    """Return (instance, raw_body) where `instance` is schema-shaped (frontmatter + context_text/solution_text)."""
    fm, body = frontmatter_io.read(path)
    sections = _first_wins(body)
    instance = dict(fm)

    for field, section_key in (("context_text", "context"), ("solution_text", "solution")):
        val = instance.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            instance[field] = sections.get(section_key, "")
        elif not isinstance(val, str):
            instance[field] = str(val)

    return instance, body
