"""`unimem redact`: remove credentials from traces already in the store."""

from __future__ import annotations

import argparse
import glob
import os
import sys
from typing import Any

from unimem import frontmatter, memory_authority, memory_guard, memory_signatures, paths, trace_io
from unimem.commands.capture_cmd import _slugify


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "redact",
        help="Remove credentials (API keys, tokens, private keys) from traces already in the "
        "store. New captures and imports are redacted as they are written.",
    )
    p.add_argument("--dest", default=None, help="Store root (default: auto-detect / $UNIMEM_ROOT)")
    p.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing.")
    p.set_defaults(func=run)


def _redact_value(value: Any, found: list[str]) -> Any:
    clean, labels = memory_guard.sanitize_metadata(value, pii=memory_guard.privacy_redaction_enabled())
    found.extend(labels)
    return clean


def trace_paths(root: str) -> list[str]:
    return sorted(
        p for p in glob.glob(os.path.join(paths.traces_dir(root), "*.md")) if os.path.basename(p) != "README.md"
    )


def redact_trace(path: str, *, write: bool, root: str | None = None) -> tuple[list[str], str]:
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
        root = root or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(path))))
        paths.enforce_boundary(root, path)
        instance, _ = trace_io.read(path)
        cleaned = _redact_value(instance, [])
        record = memory_authority.trace_record(cleaned)
        receipt = fm.get("extensions", {}).get("profile", {}).get("origin")
        if receipt:
            # An operator may amend only the sanitizer's transformation of an
            # authenticated record, never bless unrelated on-disk tampering.
            if not memory_authority.verify(root, receipt) or _redact_value(receipt["record"], []) != record:
                raise PermissionError("redaction does not match the authenticated trace content")
            receipt = memory_authority.bind(root, record, sources=record.get("source_traces", []), _redact_history=True)
            new_fm.setdefault("extensions", {}).setdefault("profile", {})["origin"] = receipt
        elif memory_authority.previously_bound(root, str(fm.get("id", ""))):
            raise PermissionError("a previously signed trace is missing its origin receipt")
        frontmatter.write(target, new_fm, new_body)
        if target != path:
            os.remove(path)
        memory_signatures.forget(root, [str(fm.get("id", ""))])
    return found, target


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    changed = 0
    total = 0
    failed = 0
    for path in trace_paths(root):
        try:
            found, target = redact_trace(path, write=not args.dry_run, root=root)
        except Exception as exc:  # noqa: BLE001 - one unreadable trace must not stop the rest
            failed += 1
            print(f"[unimem] skipped {_redact_value(path, [])}: {type(exc).__name__}", file=sys.stderr)
            continue
        if not found:
            continue
        changed += 1
        total += len(found)
        verb = "would redact" if args.dry_run else "redacted"
        moved = f" -> {os.path.basename(target)}" if target != path else ""
        safe_name = _redact_value(os.path.basename(path), [])
        print(f"{verb} {len(found)} ({', '.join(dict.fromkeys(found))}): {safe_name}{moved}")
    if not changed:
        print(
            "[unimem] no traces redacted; some traces could not be checked."
            if failed
            else "[unimem] no credentials found in this store's traces."
        )
    else:
        print(
            f"[unimem] {'would redact' if args.dry_run else 'redacted'} {total} credential(s) in "
            f"{changed} trace(s)."
            + ("" if args.dry_run else " Rotate those keys: they were readable in this store until now.")
        )
    return 1 if failed else 0
