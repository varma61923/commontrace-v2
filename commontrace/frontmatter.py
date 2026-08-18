"""Read/write Markdown files with YAML frontmatter (the CommonTrace file format)."""
from __future__ import annotations

import re
from typing import Any

import yaml

# Delimiter must be its own line (optionally trailing whitespace), not just the
# substring "---" anywhere in the file -- a plain `content.split("---", 2)` corrupts
# any field whose value happens to contain "---" (e.g. a title like "before---after").
_DELIM_RE = re.compile(r"^---[ \t]*$", re.MULTILINE)


def read(path: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body_markdown) for a `---\\nYAML\\n---\\nbody` file."""
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    if not content.startswith("---"):
        return {}, content
    delims = list(_DELIM_RE.finditer(content))
    if len(delims) < 2:
        return {}, content
    fm = yaml.safe_load(content[delims[0].end():delims[1].start()]) or {}
    body = content[delims[1].end():].lstrip("\n")
    return fm, body


def write(path: str, frontmatter: dict[str, Any], body: str) -> None:
    fm_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("---\n")
        fh.write(fm_text)
        fh.write("---\n\n")
        fh.write(body.rstrip("\n") + "\n")
