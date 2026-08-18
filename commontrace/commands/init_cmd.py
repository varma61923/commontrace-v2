from __future__ import annotations

import argparse
import os

from commontrace import paths, templates


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "init",
        help="Scaffold a local CommonTrace store (memory/) for a fleet or project.",
    )
    p.add_argument(
        "--agent-type",
        choices=paths.AGENT_TYPES,
        default="code",
        help="Kind of agent this store is for (default: code). See protocol/PROTOCOL.md#7-taxonomy-open-not-closed.",
    )
    p.add_argument("--dest", default=".", help="Directory to scaffold into (default: current directory)")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = os.path.abspath(args.dest)
    mem = paths.memory_dir(root)
    lessons = paths.lessons_dir(root)
    traces = paths.traces_dir(root)

    if os.path.isdir(mem):
        print(f"[commontrace] memory/ already exists at {mem} - leaving it as-is.")
    else:
        os.makedirs(lessons, exist_ok=True)
        os.makedirs(traces, exist_ok=True)
        if args.agent_type == "code":
            os.makedirs(paths.episodes_dir(root), exist_ok=True)

        with open(os.path.join(lessons, "lesson_template.md"), "w", encoding="utf-8", newline="\n") as fh:
            fm = templates.lesson_frontmatter(
                slug="lesson-slug",
                description="one-line summary (used by the Retriever for relevance check)",
                agent_type=args.agent_type,
                domain=paths.STARTER_DOMAINS.get(args.agent_type, ["other"])[0],
                tags=["tag1", "tag2"],
                applies_when="precise semantic activation condition (>= 1 concrete sentence, not generic)",
                do_not_apply_when="explicit counter-condition (prevents over-generalization)",
            )
            fh.write("---\n")
            fh.write(templates.dump_frontmatter(fm))
            fh.write("---\n\n")
            fh.write(templates.lesson_body())

        with open(os.path.join(traces, "README.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                "# memory/traces/ — captured experience\n\n"
                "One file per `Trace` (protocol/schemas/trace.schema.json), written by "
                "`commontrace capture`. Each Trace is a self-contained "
                "title/context/solution/tags record — the same object the CommonTrace "
                "Hub serves via `search_traces` / `get_trace`.\n\n"
                "Promote a validated pattern out of raw traces into `memory/lessons/` "
                "with `commontrace lesson new`, or push it to the Hub with "
                "`commontrace sync` (see protocol/PROTOCOL.md#5-store-two-conformance-tiers).\n"
            )

        with open(paths.index_path(root), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(templates.index_md(args.agent_type))

        print(f"[commontrace] Initialized a {args.agent_type} store at {mem}")

    print("")
    print("Next steps:")
    print(f"  commontrace lesson new --agent-type {args.agent_type} ...   # capture a validated rule")
    print(f"  commontrace capture --agent-type {args.agent_type} ...      # capture raw experience")
    print("  commontrace install --target claude-code|cursor|devin|windsurf|generic-mcp")
    print("  commontrace doctor                                          # check environment")
    return 0
