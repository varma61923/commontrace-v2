"""`commontrace redact`: remove credentials from traces already in the store.

Captures and imports redact credentials as they are written
(commontrace/memory_guard.py:redact_secrets). A store that predates that
can still hold keys that leaked into its logs -- in a trace's fields, its
body, and its filename when the key was in the title. This rewrites those
traces in place, through the same atomic writer as every other command, and
renames a file whose name was built from such a title.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from typing import Any

from commontrace import frontmatter, memory_guard, paths
from commontrace.commands.capture_cmd import _slugify


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "redact",
        help="Remove credentials (API keys, tokens, private keys) from traces already in the "
        "store. New captures and imports are redacted as they are written.",
    )
    p.add_argument("--dest", default=None, help="Store root (default: auto-detect / $COMMONTRACE_ROOT)")
    p.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing.")
    p.set_defaults(func=run)


def _redact_value(value: Any, found: list[str]) -> Any:
    if isinstance(value, str):
        clean, labels = memory_guard.redact_secrets(value)
        found.extend(labels)
        return clean
    if isinstance(value, list):
        return [_redact_value(v, found) for v in value]
    if isinstance(value, dict):
        return {k: _redact_value(v, found) for k, v in value.items()}
    return value


def trace_paths(root: str) -> list[str]:
    return sorted(
        p for p in glob.glob(os.path.join(paths.traces_dir(root), "*.md"))
        if os.path.basename(p) != "README.md"
    )


def redact_trace(path: str, *, write: bool) -> tuple[list[str], str]:
    """(kinds of credential found, the path the trace ends up at)."""
    fm, body = frontmatter.read(path)
    found: list[str] = []
    new_fm = _redact_value(fm, found)
    new_body = _redact_value(body, found)
    if not found:
        return [], path
    target = path
    old_slug, new_slug = _slugify(str(fm.get("title", ""))), _slugify(str(new_fm.get("title", "")))
    name = os.path.basename(path)
    if old_slug != new_slug and f"_{old_slug}_" in name:
        candidate = os.path.join(os.path.dirname(path), name.replace(f"_{old_slug}_", f"_{new_slug}_", 1))
        if not os.path.exists(candidate):
            target = candidate
    if write:
        frontmatter.write(target, new_fm, new_body)
        if target != path:
            os.remove(path)
    return found, target


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    changed = 0
    total = 0
    for path in trace_paths(root):
        try:
            found, target = redact_trace(path, write=not args.dry_run)
        except Exception as exc:  # noqa: BLE001 - one unreadable trace must not stop the rest
            print(f"[commontrace] skipped {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        if not found:
            continue
        changed += 1
        total += len(found)
        verb = "would redact" if args.dry_run else "redacted"
        moved = f" -> {os.path.basename(target)}" if target != path else ""
        print(f"{verb} {len(found)} ({', '.join(dict.fromkeys(found))}): {os.path.basename(path)}{moved}")
    if not changed:
        print("[commontrace] no credentials found in this store's traces.")
    else:
        print(
            f"[commontrace] {'would redact' if args.dry_run else 'redacted'} {total} credential(s) in "
            f"{changed} trace(s)."
            + ("" if args.dry_run else " Rotate those keys: they were readable in this store until now.")
        )
    return 0
