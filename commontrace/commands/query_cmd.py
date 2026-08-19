from __future__ import annotations

import argparse
import glob
import os
import sys

from commontrace import frontmatter, paths, retrieval
from commontrace.commands._shellout import has_attention_deps, run_script


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "query",
        help="Retrieve top-K relevant lessons for a task (semantic pre-filter, "
        "or a lexical fallback with only the core install).",
    )
    p.add_argument("task", help="Incoming task / query string")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument(
        "--lexical", action="store_true",
        help="Force the pure-Python lexical fallback even if the attention extra is installed.",
    )
    p.add_argument("--agent-type", default=None)
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def _iter_active_lessons(root: str, agent_type: str | None) -> list[tuple[str, dict]]:
    ldir = paths.lessons_dir(root)
    out = []
    for path in sorted(glob.glob(os.path.join(ldir, "lesson_*.md"))):
        if os.path.basename(path) == "lesson_template.md":
            continue
        fm, _ = frontmatter.read(path)
        if fm.get("status") != "active":
            continue
        if agent_type and fm.get("agent_type") != agent_type:
            continue
        out.append((path, fm))
    return out


def _run_lexical(args: argparse.Namespace, root: str) -> int:
    lessons = _iter_active_lessons(root, args.agent_type)
    ranked = retrieval.rank_lessons(args.task, lessons, top_k=args.top_k)
    if not ranked:
        print("[commontrace] no lexical matches. Try `commontrace lesson list` for a full view.")
        return 0
    for r in ranked:
        print(f"{r.slug:45s} score={r.score:5.1f}  {r.description}")
        print(f"  matched: {', '.join(r.matched_terms)}  ({r.path})")
    return 0


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)

    if args.lexical or not has_attention_deps():
        if not args.lexical:
            print(
                "[commontrace] Semantic retrieval requires the optional attention extra "
                "(numpy + sentence-transformers): `pip install commontrace[attention]`. "
                "Falling back to lexical (word-overlap) retrieval.",
                file=sys.stderr,
            )
        return _run_lexical(args, root)

    return run_script(
        root,
        os.path.join("memory", "attention", "query.py"),
        [args.task, "--top-k", str(args.top_k)],
        "Retrieval requires the reference attention scripts from the commontrace-v2 "
        "repo checkout (memory/attention/) plus `pip install commontrace[attention]`. "
        "Falling back: `commontrace query --lexical`, or `commontrace lesson list` for a full view.",
    )
