"""Read/write Markdown files with YAML frontmatter (the CommonTrace file format)."""
from __future__ import annotations

import re
from typing import Any

import yaml

# Delimiter must be its own line (optionally trailing whitespace / CR), not just the
# substring "---" anywhere in the file -- a plain `content.split("---", 2)` corrupts
# any field whose value happens to contain "---" (e.g. a title like "before---after").
# \r is allowed so a raw CRLF string parses even when it didn't come from a
# universal-newline text-mode read.
_DELIM_RE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)


class FrontmatterError(ValueError):
    """Raised when a file's frontmatter block is present but not parseable YAML,
    or does not decode to a mapping. Callers should treat this as a clean,
    reportable error rather than letting a raw yaml.YAMLError/AttributeError
    traceback reach the user."""


def read(path: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body_markdown) for a `---\\nYAML\\n---\\nbody` file."""
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    if not content.startswith("---"):
        return {}, content
    delims = list(_DELIM_RE.finditer(content))
    if len(delims) < 2:
        return {}, content
    fm_text = content[delims[0].end():delims[1].start()]
    try:
        fm = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"{path}: malformed YAML frontmatter: {exc}") from exc
    if fm is None:
        fm = {}
    if not isinstance(fm, dict):
        raise FrontmatterError(
            f"{path}: frontmatter must be a YAML mapping, got {type(fm).__name__}"
        )
    body = content[delims[1].end():].lstrip("\n")
    return fm, body


def write(path: str, frontmatter: dict[str, Any], body: str) -> None:
    fm_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("---\n")
        fh.write(fm_text)
        fh.write("---\n\n")
        fh.write(body.rstrip("\n") + "\n")
