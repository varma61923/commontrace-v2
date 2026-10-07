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


def write_new(
    root: str,
    *,
    title: str,
    context: str,
    solution: str,
    tags: list[str],
    agent_type: str | None = None,
    trace_id: str | None = None,
    outcome: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> str | None:
    """Write one schema-valid Trace with credentials redacted; returns its path."""
    import datetime
    import os
    import uuid

    from commontrace import memory_guard, paths, templates, validate
    from commontrace.commands.capture_cmd import _free_path, _id_suffix, _slugify

    clean, _ = memory_guard.sanitize_metadata(
        {"title": title, "context": context, "solution": solution, "tags": tags, "extra": extra or {}},
        pii=memory_guard.privacy_redaction_enabled(),
    )
    title = clean["title"].strip()[:200] or "Trace"
    context = clean["context"].strip()
    solution = clean["solution"].strip()
    tags = clean["tags"]
    extra = clean["extra"]
    tid = trace_id or str(uuid.uuid4())
    tdir = paths.traces_dir(root)
    os.makedirs(tdir, exist_ok=True)
    date = datetime.date.today().isoformat()
    suffix = _id_suffix(tid)
    if trace_id:
        for name in os.listdir(tdir):
            if name.endswith(f"_{suffix}.md"):
                return None
    fm = templates.trace_frontmatter(
        tid, title, agent_type or paths.store_agent_type(root),
        [str(t).strip() for t in tags if str(t).strip()], "", outcome,
    )
    fm.update(extra or {})
    instance = {**fm, "context_text": context, "solution_text": solution}
    errors = validate.validate(instance, validate.load_schema("trace.schema.json"))
    if errors:
        raise ValueError("invalid trace: " + "; ".join(errors))
    out_path = _free_path(os.path.join(tdir, f"{date}_{_slugify(title)}_{suffix}.md"), tid)
    frontmatter_io.write(out_path, fm, templates.trace_body(context, solution))
    return out_path
