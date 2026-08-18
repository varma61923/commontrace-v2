"""Read/write Markdown files with YAML frontmatter (the CommonTrace file format)."""
from __future__ import annotations

from typing import Any

import yaml


def read(path: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body_markdown) for a `---\\nYAML\\n---\\nbody` file."""
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    fm = yaml.safe_load(parts[1]) or {}
    body = parts[2].lstrip("\n")
    return fm, body


def write(path: str, frontmatter: dict[str, Any], body: str) -> None:
    fm_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("---\n")
        fh.write(fm_text)
        fh.write("---\n\n")
        fh.write(body.rstrip("\n") + "\n")
