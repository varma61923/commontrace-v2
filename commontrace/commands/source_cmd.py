"""`commontrace source`: measure memory that lives in a plain file this
product does not own -- CLAUDE.md, AGENTS.md, .cursor/rules, a Devin
Knowledge export -- through the same holdout machinery as any lesson in
this store. See commontrace/memory_sources.py for what this actually does
and why a query/retrieval-shaped adapter (commontrace/measure.py) does not
fit a file that is read whole, unconditionally, on every session.
"""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import holdout_io, memory_sources, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "source",
        help="Causally measure file-based agent memory (CLAUDE.md, AGENTS.md, "
        ".cursor/rules) that this product does not itself store.",
    )
    sub = p.add_subparsers(dest="source_cmd", required=True)

    sections = sub.add_parser(
        "sections", help="List the ## sections this file would be split into.",
    )
    sections.add_argument("path", help="Path to the memory file, e.g. CLAUDE.md")
    sections.add_argument("--json", action="store_true")
    sections.add_argument("--dest", default=None)
    sections.set_defaults(func=run_sections)

    render = sub.add_parser(
        "render",
        help="Render this occasion's version of the file: eligible sections "
        "not drawn into the withheld arm, blocked sections always excluded.",
    )
    render.add_argument("path")
    render.add_argument(
        "--occasion-id", required=True,
        help="Stable id for this session/task. The same id always gets the same "
        "answer; record its outcome afterwards with `source outcome`.",
    )
    render.add_argument("--out", default=None, help="Write the rendered text here instead of stdout.")
    render.add_argument("--dest", default=None)
    render.set_defaults(func=run_render)

    outcome = sub.add_parser(
        "outcome", help="Report whether the occasion this file was rendered for succeeded.",
    )
    outcome.add_argument("--occasion-id", required=True)
    group = outcome.add_mutually_exclusive_group(required=True)
    group.add_argument("--succeeded", action="store_true")
    group.add_argument("--failed", action="store_true")
    outcome.add_argument("--dest", default=None)
    outcome.set_defaults(func=run_outcome)

    withdraw = sub.add_parser(
        "withdraw",
        help="Permanently exclude a section from every future render (harm withdrawal). "
        "Never edits the source file -- see commontrace/memory_sources.py.",
    )
    withdraw.add_argument("path")
    withdraw.add_argument("--section-id", required=True)
    withdraw.add_argument("--dest", default=None)
    withdraw.set_defaults(func=run_withdraw)

    reinstate = sub.add_parser(
        "reinstate", help="Undo `withdraw` for one section.",
    )
    reinstate.add_argument("path")
    reinstate.add_argument("--section-id", required=True)
    reinstate.add_argument("--dest", default=None)
    reinstate.set_defaults(func=run_reinstate)


def run_sections(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    source = memory_sources.FileMemorySource(args.path, root=root)
    try:
        sections = source.sections()
    except OSError as exc:
        print(f"[commontrace] cannot read {args.path!r}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([{"id": s.id, "heading": s.heading} for s in sections], indent=2))
        return 0

    if not sections:
        print(f"[commontrace] no '## ' sections found in {args.path} (nothing to measure separately).")
        return 0
    for s in sections:
        print(f"  {s.id:<40} {s.heading}")
    return 0


def run_render(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    source = memory_sources.FileMemorySource(args.path, root=root)
    try:
        result = source.render(args.occasion_id)
    except OSError as exc:
        print(f"[commontrace] cannot read {args.path!r}: {exc}", file=sys.stderr)
        return 1

    if args.out:
        try:
            safe_out = paths.safe_prepare_output_path(args.out)
        except (OSError, ValueError) as exc:
            print(f"[commontrace] could not write {args.out!r}: {exc}", file=sys.stderr)
            return 1
        with open(safe_out, "w", encoding="utf-8") as fh:
            fh.write(result.text)
        print(f"[commontrace] wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(result.text)

    if result.blocked:
        print(f"[commontrace] blocked (harm-withdrawn, never drawn): {', '.join(result.blocked)}", file=sys.stderr)
    if result.withheld:
        print(f"[commontrace] withheld for this occasion: {', '.join(result.withheld)}", file=sys.stderr)
    print(
        f"[commontrace] record the outcome with: "
        f"commontrace source outcome --occasion-id {args.occasion_id} --succeeded|--failed",
        file=sys.stderr,
    )
    return 0


def run_outcome(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        wrote = holdout_io.record_outcome(root, args.occasion_id, bool(args.succeeded))
    except holdout_io.ConflictingOutcome as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    if wrote:
        print(f"[commontrace] recorded occasion {args.occasion_id!r} as "
              f"{'succeeded' if args.succeeded else 'failed'}.")
    else:
        print(f"[commontrace] occasion {args.occasion_id!r} was already recorded the same way.")
    return 0


def run_withdraw(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    memory_sources.withdraw(root, args.path, args.section_id)
    print(f"[commontrace] {args.section_id!r} in {args.path} is now blocked from every future render.")
    return 0


def run_reinstate(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    if memory_sources.reinstate(root, args.path, args.section_id):
        print(f"[commontrace] {args.section_id!r} in {args.path} is eligible again.")
    else:
        print(f"[commontrace] {args.section_id!r} in {args.path} was not blocked.")
    return 0
