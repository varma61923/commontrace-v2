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
        default=None,
        help="Business function kit: support, sales, hr, coding, marketing, robotics, legal, "
             "finance, clinical (`commontrace function list`), or 'custom' to take the slug "
             f"from --agent-type. Without --function or --agent-type the store is "
             f"'{paths.GENERAL_AGENT_TYPE}'.",
    )
    p.add_argument(
        "--kit", default=None, metavar="FILE",
        help="A function kit spec (JSON) for a function not built in; "
             "validate it first with `commontrace function check`.",
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
    p.add_argument(
        "--git", action="store_true",
        help="Make the store its own git repository, so forgetting or restoring a fact "
             "is recorded as a commit. Refused when the store sits inside another repository.",
    )
    p.add_argument("--dest", default=".", help="Directory to scaffold into (default: current directory)")
    p.add_argument("--agent", nargs="?", const="new", help="Mint a scoped local agent key after initialization.")
    p.add_argument("--agent-caller", default=None, metavar="NAME",
                   help="With --agent: which assistant is signing up (e.g. claude-code). Names a new "
                        "agent '<NAME>-<random>' and records the scope 'caller:<NAME>'. The key prints once.")
    p.set_defaults(func=run)


EPISODE_PROFILES = frozenset({"code-review"})


def _init_git(root: str) -> int:
    from commontrace import memory_git

    if memory_git.is_repo(root) and not memory_git.owns_repo(root):
        print(
            "[commontrace] --git refused: this store is inside another git repository, and "
            "audit commits there would mix memory changes with that project's history. "
            "Keep the store in its own directory to version it.",
            file=sys.stderr,
        )
        return 2
    status = memory_git.init_repo(root)
    if not status.get("ok"):
        print(f"[commontrace] --git failed: {status.get('error', 'unknown error')}", file=sys.stderr)
        return 1
    memory_git.commit_all(root, "commontrace: initialize store")
    print(f"[commontrace] Store is versioned with git at {root}")
    return 0


def _profile_for(args: argparse.Namespace) -> str:
    if args.profile is not None:
        return args.profile.strip()
    return "code-review" if args.agent_type == "code" else ""


def resolve_kit(function: str | None, kit_file: str | None):
    """The kit named by --function / --kit, or None. Raises functions.KitError."""
    from commontrace import functions

    if function and kit_file:
        raise functions.KitError("pass --function or --kit, not both")
    if kit_file:
        return functions.load_file(kit_file)
    if function and function != "custom":
        return functions.resolve(function)
    return None


def resolve_agent_type(function: str | None, agent_type: str | None, kit=None) -> str:
    """The agent_type to stamp on the store. Raises ValueError on a conflict."""
    if kit is None:
        return agent_type or (function and "custom") or paths.GENERAL_AGENT_TYPE
    if agent_type and agent_type != kit.agent_type:
        raise ValueError(
            f"function {kit.key!r} stores as agent_type {kit.agent_type!r}, "
            f"which conflicts with --agent-type {agent_type!r}"
        )
    return kit.agent_type


def run(args: argparse.Namespace) -> int:
    try:
        kit = resolve_kit(args.function, args.kit)
        args.agent_type = resolve_agent_type(args.function, args.agent_type, kit)
    except ValueError as exc:
        print(f"[commontrace] error: {exc}", file=sys.stderr)
        return 2
    root = os.path.abspath(args.dest)
    if kit is not None:
        paths.STARTER_DOMAINS.setdefault(kit.agent_type, list(kit.domains) or ["other"])
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

        if kit is not None:
            from commontrace import functions

            functions.save_to_store(root, kit)
        print(f"[commontrace] Initialized a {args.agent_type} store at {mem}")
        if kit is not None:
            print(f"  Occasion: one {kit.occasion_label} (e.g. {kit.occasion_example}). "
                  f"Success: {kit.outcome.success}.")
            print(f"  `commontrace function forecast {kit.key} --daily <occasions per day>` "
                  "says how long a verdict takes at your volume.")

    if getattr(args, "agent_caller", None) and not getattr(args, "agent", None):
        args.agent = "new"  # naming the caller is asking for a new agent key
    if getattr(args, "agent", None):
        import json
        import re
        import uuid

        from commontrace import onboarding

        caller = getattr(args, "agent_caller", None)
        if caller is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,40}", caller):
            print("[commontrace] --agent-caller must be 1-41 letters, digits, '.', '_' or '-'.", file=sys.stderr)
            return 2
        prefix = caller or "agent"
        agent_id = prefix+"-"+uuid.uuid4().hex[:12] if args.agent == "new" else args.agent
        try:
            result = onboarding.install(root, agent_id, commits=0,
                                        labels=["caller:" + caller] if caller else None)
        except ValueError as exc:
            print(f"[commontrace] --agent refused: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result))

    if getattr(args, "git", False):
        rc = _init_git(root)
        if rc:
            return rc

    if has_attention_deps():
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
