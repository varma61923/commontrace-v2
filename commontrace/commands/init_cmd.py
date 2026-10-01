from __future__ import annotations

import argparse
import os
import sys

from commontrace import paths, templates
from commontrace.commands import _validators
from commontrace.commands._shellout import has_attention_deps


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "init",
        help="Scaffold a local CommonTrace store (memory/) for a fleet or project.",
    )
    p.add_argument(
        "--function",
        choices=(*paths.FUNCTION_AGENT_TYPES, "custom"),
        default=None,
        help="Business function this store is for. 'custom' takes its slug from "
             "--agent-type. Without --function or --agent-type the store is "
             f"'{paths.GENERAL_AGENT_TYPE}'.",
    )
    p.add_argument(
        "--agent-type",
        type=_validators.agent_type,
        default=None,
        help="Kind of agent this store is for, as a lowercase slug (default: "
             f"{paths.GENERAL_AGENT_TYPE}). Any field works -- e.g. code, support, sales, "
             "hr, marketing, ops, robotics, legal. The taxonomy is open: see "
             "protocol/PROTOCOL.md#7-taxonomy-open-not-closed.",
    )
    p.add_argument(
        "--profile", default=None,
        help="Reference pipeline this store runs, if any. 'code-review' (SKILL.md's "
             "Implementer/Reviewer loop) is the one that writes memory/episodes/, so "
             "naming it here scaffolds that directory. Defaults to 'code-review' for "
             "--agent-type code, and to no profile otherwise.",
    )
    p.add_argument("--dest", default=".", help="Directory to scaffold into (default: current directory)")
    p.set_defaults(func=run)


# The profiles whose pipelines write memory/episodes/ rather than
# memory/traces/. Episodes are a property of the PROFILE, not of the fleet:
# SKILL.md's double-review loop emits them, and any fleet could in principle
# run that loop. Keying the store layout on `agent_type == "code"` instead
# made "code" the only first-class agent type in a product whose taxonomy is
# explicitly open (protocol/PROTOCOL.md#7).
EPISODE_PROFILES = frozenset({"code-review"})


def _profile_for(args: argparse.Namespace) -> str:
    if args.profile is not None:
        return args.profile.strip()
    # The code-review profile is what writes episodes/, so it follows the
    # coding agent type and nothing else.
    return "code-review" if args.agent_type == "code" else ""


def resolve_agent_type(function: str | None, agent_type: str | None) -> str:
    """The agent_type to stamp on the store. Raises ValueError on a conflict."""
    if function in (None, "custom"):
        return agent_type or (function and "custom") or paths.GENERAL_AGENT_TYPE
    expected = paths.FUNCTION_AGENT_TYPES[function]
    if agent_type and agent_type != expected:
        raise ValueError(
            f"--function {function} stores as agent_type {expected!r}, "
            f"which conflicts with --agent-type {agent_type!r}"
        )
    return expected


def run(args: argparse.Namespace) -> int:
    try:
        args.agent_type = resolve_agent_type(args.function, args.agent_type)
    except ValueError as exc:
        print(f"[commontrace] error: {exc}", file=sys.stderr)
        return 2
    root = os.path.abspath(args.dest)
    profile = _profile_for(args)
    mem = paths.memory_dir(root)
    lessons = paths.lessons_dir(root)
    traces = paths.traces_dir(root)

    if os.path.isdir(mem):
        print(f"[commontrace] memory/ already exists at {mem} - leaving it as-is.")
    else:
        os.makedirs(lessons, exist_ok=True)
        os.makedirs(traces, exist_ok=True)
        attention = os.path.join(mem, "attention")
        os.makedirs(attention, exist_ok=True)
        if profile in EPISODE_PROFILES:
            os.makedirs(paths.episodes_dir(root), exist_ok=True)

        attention_readme = os.path.join(attention, "README.md")
        if not os.path.exists(attention_readme):
            with open(attention_readme, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(
                    "# memory/attention/ — semantic retrieval\n\n"
                    "Contains the semantic embedding index (`index.npz`) used by "
                    "`commontrace query` and `memory/attention/query.py`.\n"
                )
        try:
            import numpy as np
            index_file = os.path.join(attention, "index.npz")
            if not os.path.exists(index_file):
                np.savez(
                    index_file,
                    slugs=np.array([], dtype=str),
                    embeddings=np.zeros((0, 768), dtype=np.float32),
                    # An empty index pins no model: the first build uses
                    # the default (build_index.index_model).
                    model_name="Snowflake/snowflake-arctic-embed-m-v1.5",
                    encoded_field="description+domain+tags+applies_when+do_not_apply_when+rule",
                    timestamp="",
                    n_lessons=0,
                )
        except Exception:
            pass

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
            fh.write(templates.index_md(
                args.agent_type,
                has_episodes=os.path.isdir(paths.episodes_dir(root)),
            ))

        print(f"[commontrace] Initialized a {args.agent_type} store at {mem}")

    if has_attention_deps():
        # Recommended, not switched on: whether a store fuses must be its
        # recorded decision, not a side effect of what happens to be
        # installed on the machine that ran `init`.
        print(
            "[commontrace] The attention extra is installed, so retrieval searches by "
            "keyword and meaning and a cross-encoder decides what reaches the page "
            "(gated fusion). The first retrieval embeds the store's lessons. Where "
            "retrieval latency matters more than accuracy, use the fast model:\n"
            "  commontrace retrieval --rerank cross-encoder-fast"
        )

    print()
    print("Next steps:")
    print(f"  commontrace lesson new --agent-type {args.agent_type} ...   # capture a validated rule")
    print(f"  commontrace capture --agent-type {args.agent_type} ...      # capture raw experience")
    print("  commontrace install --target claude-code|cursor|devin|windsurf|generic-mcp")
    print("  commontrace doctor                                          # check environment")
    return 0
