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

_SECTION_RE = re.compile(r"^##\s*(Context|Solution)\s*\n(.*?)(?=\n##\s|\Z)", re.S | re.M)


def read(path: str) -> tuple[dict[str, Any], str]:
    """Return (instance, raw_body) where `instance` is schema-shaped (frontmatter + context_text/solution_text)."""
    fm, body = frontmatter_io.read(path)
    sections = {m.group(1).lower(): m.group(2).strip() for m in _SECTION_RE.finditer(body)}
    instance = dict(fm)
    instance.setdefault("context_text", sections.get("context", ""))
    instance.setdefault("solution_text", sections.get("solution", ""))
    return instance, body
