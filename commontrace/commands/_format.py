"""Shared rendering helpers for the listing commands."""
from __future__ import annotations

import sys

from commontrace import frontmatter


def read_or_warn(read_fn, path: str):
    """Call `read_fn(path)` (frontmatter.read or trace_io.read), returning
    None and printing a warning to stderr instead of raising when the file
    is corrupt or unreadable.

    Every listing/aggregation command in commontrace/commands/ globs a
    directory of hand-editable Markdown files and reads each one in a loop
    -- query_cmd, lesson_cmd, trace_cmd, reliability_cmd, commons_cmd,
    overlap_cmd, experiment_cmd. Without this, one file a person left
    mid-edit (bad YAML, a stray null byte, whatever) raised out of the loop
    and aborted the WHOLE command -- including reporting on every other,
    perfectly fine file in the store. `lesson validate`/`trace validate`
    already treat this as a per-file, not per-run, failure; every other
    command that reads the same files should fail exactly as gracefully.
    """
    try:
        return read_fn(path)
    except (frontmatter.FrontmatterError, OSError, UnicodeDecodeError) as exc:
        print(f"[commontrace] warning: skipping unreadable file {path}: {exc}", file=sys.stderr)
        return None


def cell(value: object, placeholder: str = "?") -> str:
    """Render one frontmatter value for a fixed-width listing column.

    `dict.get(key, default)` returns the default only when the key is
    ABSENT. A key that is present with an empty YAML value (`status:`) parses
    to None, and None has no `__format__`, so a width spec like `:8s` raises
    TypeError. A half-finished hand edit is exactly when someone runs a
    listing to find the file that needs fixing, so the listing must survive
    it -- `lesson validate` already reports such a file cleanly, and the two
    commands disagreeing is the actual defect.

    Absent and present-but-empty are treated identically: both mean "nothing
    usable here".
    """
    return placeholder if value is None or value == "" else str(value)
