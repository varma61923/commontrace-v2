"""Shared rendering helpers for the listing commands."""
from __future__ import annotations

import os
import sys

from commontrace import frontmatter


def resolve_hub(args) -> tuple[str, str] | None:
    """Resolve a Hub URL + API key from --hub-url/--hub-api-key or the
    COMMONTRACE_HUB_URL/COMMONTRACE_HUB_API_KEY environment variables,
    warning (not refusing) when the key came from the command line rather
    than the environment. Shared by every command module that talks to a
    Hub (commons_cmd, account_cmd) so the warning and the missing-config
    message stay in exactly one place.
    """
    hub_url = args.hub_url or os.environ.get("COMMONTRACE_HUB_URL")
    api_key = args.hub_api_key or os.environ.get("COMMONTRACE_HUB_API_KEY")
    if args.hub_api_key:
        # A CLI argument is readable by any local user via `ps`/
        # `/proc/<pid>/cmdline`, and can land in shell history and
        # auditd's process-exec logs -- none of which apply to an
        # environment variable set via COMMONTRACE_HUB_API_KEY. --help
        # already says the env var is preferred; this is the same warning
        # at the moment it actually matters; someone who ran the command
        # is more likely to see it than someone who read --help first.
        print(
            "[commontrace] [WARN] --hub-api-key was passed on the command line, which is "
            "visible to other local users (`ps`, /proc, shell history). Prefer setting "
            "COMMONTRACE_HUB_API_KEY instead.",
            file=sys.stderr,
        )
    if not hub_url or not api_key:
        print(
            "[commontrace] a Hub URL and API key are required.\n"
            "  Set COMMONTRACE_HUB_URL and COMMONTRACE_HUB_API_KEY, or pass\n"
            "  --hub-url / --hub-api-key.",
            file=sys.stderr,
        )
        return None
    return hub_url, api_key


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
