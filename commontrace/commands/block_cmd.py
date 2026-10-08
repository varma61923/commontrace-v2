"""`commontrace block`: manage stateful agent working memory blocks."""
from __future__ import annotations

import argparse
import sys

from commontrace import memory_blocks, paths


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "block",
        help="Manage stateful agent working memory blocks (persona, human, project, guidelines).",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_list = sub.add_parser("list", help="List all configured memory blocks.")
    p_list.add_argument("--dest", default=None)
    p_list.set_defaults(func=run_list)

    p_get = sub.add_parser("get", help="Show the content and metadata of a memory block.")
    p_get.add_argument("name", help="Name of the memory block (e.g. persona, human, project).")
    p_get.add_argument("--dest", default=None)
    p_get.set_defaults(func=run_get)

    p_set = sub.add_parser("set", help="Create or overwrite a memory block.")
    p_set.add_argument("name", help="Name of the memory block.")
    p_set.add_argument("content", help="New text content for the block.")
    p_set.add_argument("--max-chars", type=int, default=memory_blocks.DEFAULT_MAX_CHARS)
    p_set.add_argument("--actor", default="cli", help="Actor responsible for the change.")
    p_set.add_argument("--reason", default="", help="Rationale for the update.")
    p_set.add_argument("--read-only", action="store_true", help="Mark the block read-only (immutable).")
    p_set.add_argument("--dest", default=None)
    p_set.set_defaults(func=run_set)

    p_app = sub.add_parser("append", help="Append text to an existing memory block.")
    p_app.add_argument("name", help="Name of the memory block.")
    p_app.add_argument("text", help="Text to append.")
    p_app.add_argument("--actor", default="cli")
    p_app.add_argument("--reason", default="")
    p_app.add_argument("--dest", default=None)
    p_app.set_defaults(func=run_append)

    p_rep = sub.add_parser("replace", help="Replace exact text within a memory block.")
    p_rep.add_argument("name", help="Name of the memory block.")
    p_rep.add_argument("--old", required=True, help="Exact substring to find.")
    p_rep.add_argument("--new", required=True, help="Replacement text.")
    p_rep.add_argument("--actor", default="cli")
    p_rep.add_argument("--reason", default="")
    p_rep.add_argument("--dest", default=None)
    p_rep.set_defaults(func=run_replace)

    p_hist = sub.add_parser("history", help="Show revision audit history.")
    p_hist.add_argument("name", nargs="?", default="", help="Optional block name to filter history.")
    p_hist.add_argument("--dest", default=None)
    p_hist.set_defaults(func=run_history)

    p_ins = sub.add_parser("insert", help="Insert text at a line of a memory block.")
    p_ins.add_argument("name", help="Name of the memory block.")
    p_ins.add_argument("text", help="Text to insert.")
    p_ins.add_argument("--line", type=int, default=-1,
                       help="0=top, -1=bottom (default), N=after line N.")
    p_ins.add_argument("--actor", default="cli")
    p_ins.add_argument("--reason", default="")
    p_ins.add_argument("--dest", default=None)
    p_ins.set_defaults(func=run_insert)

    p_render = sub.add_parser("render", help="Render blocks as XML for prompt injection.")
    p_render.add_argument("--dest", default=None)
    p_render.set_defaults(func=run_render)

    p_del = sub.add_parser("delete", help="Delete a memory block.")
    p_del.add_argument("name", help="Name of the memory block to delete.")
    p_del.add_argument("--actor", default="cli")
    p_del.add_argument("--reason", default="")
    p_del.add_argument("--dest", default=None)
    p_del.set_defaults(func=run_delete)

    for mutation in (p_set, p_app, p_rep, p_ins, p_del):
        mutation.add_argument(
            "--expected-revision", default=None,
            help="Only mutate this revision; an empty string requires a missing block.",
        )


def run_list(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    blocks = memory_blocks.list_blocks(root)
    if not blocks:
        print("No memory blocks configured. Run `commontrace block set <name> <content>` to create one.")
        return 0

    print(f"{'NAME':<15} {'CHARS':<10} {'QUOTA':<10} {'REVISION':<18} {'UPDATED'}")
    print("-" * 75)
    for b in blocks:
        print(f"{b.name:<15} {b.char_count:<10} {b.max_chars:<10} {b.revision:<18} {b.updated_at[:19]}")
    return 0


def run_get(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        b = memory_blocks.get_block(root, args.name)
    except memory_blocks.BlockNotFoundError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print(f"# Block: {b.name} (rev: {b.revision}, chars: {b.char_count}/{b.max_chars})")
    print(f"# Updated: {b.updated_at}")
    print("-" * 60)
    print(b.content)
    return 0


def run_set(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        b = memory_blocks.set_block(
            root=root,
            name=args.name,
            content=args.content,
            max_chars=args.max_chars,
            actor=args.actor,
            reason=args.reason,
            read_only=bool(getattr(args, "read_only", False)),
            expected_revision=getattr(args, "expected_revision", None),
        )
    except memory_blocks.MemoryBlockError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print(f"Saved block '{b.name}' (rev: {b.revision}, {b.char_count}/{b.max_chars} chars).")
    return 0


def run_append(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        b = memory_blocks.append_block(
            root=root,
            name=args.name,
            text=args.text,
            actor=args.actor,
            reason=args.reason,
            expected_revision=getattr(args, "expected_revision", None),
        )
    except memory_blocks.MemoryBlockError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print(f"Appended to block '{b.name}' (rev: {b.revision}, {b.char_count}/{b.max_chars} chars).")
    return 0


def run_replace(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        b = memory_blocks.replace_block(
            root=root,
            name=args.name,
            old_str=args.old,
            new_str=args.new,
            actor=args.actor,
            reason=args.reason,
            expected_revision=getattr(args, "expected_revision", None),
        )
    except memory_blocks.MemoryBlockError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print(f"Updated block '{b.name}' (rev: {b.revision}, {b.char_count}/{b.max_chars} chars).")
    return 0


def run_history(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    entries = memory_blocks.block_history(root, args.name)
    if not entries:
        print("No revision history recorded.")
        return 0

    print(f"{'TIMESTAMP':<22} {'BLOCK':<12} {'ACTION':<10} {'ACTOR':<10} {'REVISION':<18} {'REASON'}")
    print("-" * 85)
    for e in entries:
        ts = e.get("timestamp", "")[:19]
        blk = e.get("block", "")
        act = e.get("action", "")
        actor = e.get("actor", "")
        rev = e.get("revision", "")
        reason = e.get("reason", "")
        print(f"{ts:<22} {blk:<12} {act:<10} {actor:<10} {rev:<18} {reason}")
    return 0


def run_delete(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        ok = memory_blocks.delete_block(
            root, args.name, actor=args.actor, reason=args.reason,
            expected_revision=getattr(args, "expected_revision", None),
        )
    except memory_blocks.MemoryBlockError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    if not ok:
        print(f"Block '{args.name}' not found.", file=sys.stderr)
        return 1
    print(f"Deleted block '{args.name}'.")
    return 0


def run_insert(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        b = memory_blocks.insert_block(
            root=root,
            name=args.name,
            text=args.text,
            line_number=args.line,
            actor=args.actor,
            reason=args.reason,
            expected_revision=getattr(args, "expected_revision", None),
        )
    except memory_blocks.MemoryBlockError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    print(f"Inserted into block '{b.name}' (rev: {b.revision}, {b.char_count}/{b.max_chars} chars).")
    return 0


def run_render(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    blocks = memory_blocks.list_blocks(root)
    print(memory_blocks.render_memory_blocks(blocks))
    return 0
