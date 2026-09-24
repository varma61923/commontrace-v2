from __future__ import annotations

import argparse
import os

from commontrace import paths, templates
from commontrace.commands import _validators
from commontrace.commands._shellout import has_attention_deps


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "init",
        help="Scaffold a local CommonTrace store (memory/) for a fleet or project.",
    )
    p.add_argument(
        "--agent-type",
        type=_validators.agent_type,
        default="code",
        help="Kind of agent this store is for, as a lowercase slug (default: code). "
             "Any field works -- e.g. code, support, sales, hr, marketing, ops, "
             "robotics, legal. The taxonomy is open: see "
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
    # Preserves the historical default exactly: `commontrace init` with no
    # arguments has always scaffolded episodes/, and the code-review profile
    # is why.
    return "code-review" if args.agent_type == "code" else ""


def run(args: argparse.Namespace) -> int:
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
                    model_name="multi-qa-mpnet-base-dot-v1",
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
            "keyword and meaning and a fast cross-encoder decides what reaches the page "
            "(gated fusion). The first retrieval embeds the store's lessons. For the "
            "most accurate retrieval, use the full model:\n"
            "  commontrace retrieval --rerank cross-encoder"
        )

    print()
    print("Next steps:")
    print(f"  commontrace lesson new --agent-type {args.agent_type} ...   # capture a validated rule")
    print(f"  commontrace capture --agent-type {args.agent_type} ...      # capture raw experience")
    print("  commontrace install --target claude-code|cursor|devin|windsurf|generic-mcp")
    print("  commontrace doctor                                          # check environment")
    return 0
