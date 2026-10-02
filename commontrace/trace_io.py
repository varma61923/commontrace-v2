"""Read a Trace file as the full object protocol/schemas/trace.schema.json describes."""
from __future__ import annotations

import re
from typing import Any

from commontrace import frontmatter as frontmatter_io

_SECTION_RE = re.compile(
    r"^##\s*(Context|Solution)\s*\n(.*?)(?=\n##\s*(?:Context|Solution)\s*\n|\Z)",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)


def _first_wins(body: str) -> dict[str, str]:
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
