"""Read/write Markdown files with YAML frontmatter (the CommonTrace file format)."""
from __future__ import annotations

import os
import re
import tempfile
from typing import Any

import yaml

# Delimiter must be its own line (optionally trailing whitespace / CR), not just the
# substring "---" anywhere in the file -- a plain `content.split("---", 2)` corrupts
# any field whose value happens to contain "---" (e.g. a title like "before---after").
# \r is allowed so a raw CRLF string parses even when it didn't come from a
# universal-newline text-mode read.
_DELIM_RE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)


class _StrictBoolLoader(yaml.SafeLoader):
    """SafeLoader, but a bare `yes`/`no`/`on`/`off` (any case) resolves as
    the plain string it looks like, not a bool.

    Every field this protocol declares as a string -- agent_type, domain,
    tags entries, a lesson's applies_when -- is one PyYAML's default
    resolver can silently misparse: `domain: NO` or `tags: [on, off]`
    become {'domain': False} / {'tags': [True, False]} with no error,
    because YAML 1.1 treats those tokens as booleans. `commontrace`'s own
    write path (yaml.safe_dump) auto-quotes them on output, so a
    round-tripped file is never at risk -- the exposure is a *hand-edited*
    file, which this protocol explicitly relies on (e.g. `lesson approve`/
    `reject` instruct manual edits).

    Rebuilds the resolver table on a SUBCLASS rather than mutating
    `yaml.SafeLoader.yaml_implicit_resolvers` in place: that dict is
    process-global, so patching it here would silently change bool
    resolution for every other `yaml.safe_load` call anywhere in the
    process (including third-party code), not just this module's reads.
    """

    yaml_implicit_resolvers = {
        first_char: [
            (tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"
        ]
        for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


_StrictBoolLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


class FrontmatterError(ValueError):
    """Raised when a file's frontmatter block is present but not parseable YAML,
    does not decode to a mapping, or the file cannot be opened at all.
    Callers should treat this as a clean, reportable error rather than letting
    a raw yaml.YAMLError/AttributeError/OSError traceback reach the user."""


def read(path: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body_markdown) for a `---\nYAML\n---\nbody` file."""
    # A path the user typed -- `lesson validate /nope/x.md`, or a directory
    # passed where a file was meant -- is a user error, not a crash. Every
    # other error path in this CLI prints "[commontrace] ..." and exits
    # non-zero; letting a raw FileNotFoundError/IsADirectoryError through
    # made this the odd one out.
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            content = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise FrontmatterError(f"cannot read {path}: {exc}") from exc
    if not content.startswith("---"):
        return {}, content
    delims = list(_DELIM_RE.finditer(content))
    if len(delims) < 2:
        return {}, content
    fm_text = content[delims[0].end():delims[1].start()]
    try:
        fm = yaml.load(fm_text, Loader=_StrictBoolLoader)
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
    target_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(target_dir, exist_ok=True)
    temp_file = tempfile.NamedTemporaryFile(
        dir=target_dir,
        delete=False,
        mode="w",
        encoding="utf-8",
        newline="\n",
    )
    temp_path = temp_file.name
    try:
        with temp_file as fh:
            fh.write("---\n")
            fh.write(fm_text)
            fh.write("---\n\n")
            fh.write(body.rstrip("\n") + "\n")
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise
