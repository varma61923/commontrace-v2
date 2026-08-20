from __future__ import annotations

import argparse
import json
import os
import shutil

from commontrace import paths

TARGETS = ["claude-code", "cursor", "devin", "windsurf", "generic-mcp", "generic"]

_HUB_TOOLS = [
    "search_traces", "contribute_trace", "get_trace", "vote_trace", "amend_trace", "list_tags",
]

_GENERIC_POINTER_SKILL = """---
name: commontrace
description: "Pointer skill — no reference pipeline (SKILL.md) was found on disk. \
Read protocol/PROTOCOL.md for the CommonTrace Protocol spec and implement \
Capture -> Structure -> Extract -> Validate -> Store -> Inject -> Measure for this agent."
---

# commontrace (generic pointer)

No `SKILL.md` (the code-review reference profile) was found next to this
install. This agent should instead:

1. Read `protocol/PROTOCOL.md` for the object model (`Trace`, `Lesson`) and
   pipeline stages.
2. Before acting: query `memory/lessons/` (or the Hub via `search_traces`)
   for lessons whose `applies_when` matches the current task.
3. After acting: capture what happened as a `Trace` (`commontrace capture`
   or `contribute_trace` on the Hub), and periodically distill repeated
   patterns into `Lesson`s (`commontrace lesson new`).
"""


def _hub_mcp_example() -> str:
    # Built via json.dumps (not an f-string template) so the generated file is guaranteed
    # valid JSON even though the comment text below embeds a quoted tool list -- a raw
    # f-string previously let those quotes leak in unescaped and break parsing.
    tools = ", ".join(_HUB_TOOLS)
    doc = {
        "_comment": (
            "Template — fill in your org's CommonTrace Hub connection details, then merge "
            "the 'commontrace' entry into your agent platform's mcp.json / mcp_servers "
            f"config. Tool surface: [{tools}]. The Hub speaks streamable-HTTP, so replace "
            "<your-hub-host> with your Hub's host and <your-api-key> with the key from "
            "`python -m hub.manage issue-key`. This file is a template only, but once you "
            "fill in a real endpoint/API key below, add its filename (or your real "
            "mcp.json) to .gitignore before committing — do not check in Hub credentials."
        ),
        "mcpServers": {
            # The Hub speaks streamable-HTTP (hub/main.py serves MCP at
            # HUB_HOST:HUB_PORT/mcp), so this is the http transport shape --
            # url + headers -- not the stdio `command`/`args`/`env` shape. A
            # stdio block here cannot carry an endpoint or a bearer token, so
            # anyone pasting it would simply fail to connect.
            "commontrace": {
                "type": "http",
                "url": "https://<your-hub-host>/mcp",
                "headers": {"Authorization": "Bearer <your-api-key>"},
            }
        },
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "install",
        help="Wire a specific agent platform to read/write this store (or the Hub).",
    )
    p.add_argument("--target", choices=TARGETS, required=True)
    p.add_argument("--dest", default=".", help="Project directory to install into (default: current directory)")
    p.set_defaults(func=run)


def _find_skill_md(root: str, dest: str) -> str | None:
    candidates = [
        os.path.join(root, "SKILL.md"),
        os.path.join(os.getcwd(), "SKILL.md"),
        os.path.join(dest, "SKILL.md"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _write(path: str, content: str) -> None:
    if os.path.isfile(path):
        print(f"  [WARN] overwriting existing file: {path}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    print(f"  wrote {path}")


def _copy(src: str, dest: str) -> None:
    if os.path.isfile(dest):
        print(f"  [WARN] overwriting existing file: {dest}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(src, dest)
    print(f"  copied {src} -> {dest}")


def _print_hub_credential_warning(example_path: str) -> None:
    print(
        f"  [WARN] {example_path} is a template for Hub connection details. "
        "Once you fill in a real endpoint/API key, make sure that file (or wherever "
        "you merge it, e.g. mcp.json) is covered by .gitignore before committing."
    )


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root()
    dest = os.path.abspath(args.dest)
    skill_md = _find_skill_md(root, dest)
    print(f"[commontrace] installing target='{args.target}' into {dest}")

    if args.target == "claude-code":
        out = os.path.join(dest, ".claude", "skills", "commontrace", "SKILL.md")
        if skill_md:
            _copy(skill_md, out)
        else:
            _write(out, _GENERIC_POINTER_SKILL)
        print("  Invoke with: /commontrace <task description + success criteria>")

    elif args.target == "devin":
        out = os.path.join(dest, ".devin", "skills", "commontrace", "SKILL.md")
        if skill_md:
            _copy(skill_md, out)
        else:
            _write(out, _GENERIC_POINTER_SKILL)

    elif args.target == "cursor":
        out = os.path.join(dest, ".cursor", "rules", "commontrace.mdc")
        _write(
            out,
            "---\n"
            "description: CommonTrace fleet memory — check before acting, capture after\n"
            "alwaysApply: false\n"
            "---\n\n"
            "Before starting a non-trivial task, check `memory/lessons/` (or the "
            "CommonTrace Hub via `search_traces`) for lessons whose `applies_when` "
            "matches this task. After finishing, capture what happened with "
            "`commontrace capture` so the next run benefits. Spec: protocol/PROTOCOL.md.\n",
        )
        example = os.path.join(dest, "commontrace.hub.mcp.json.example")
        _write(example, _hub_mcp_example())
        print(f"  To connect to the Hub: merge {example} into .cursor/mcp.json")
        _print_hub_credential_warning(example)

    elif args.target == "windsurf":
        out = os.path.join(dest, ".windsurf", "rules", "commontrace.md")
        _write(
            out,
            "# CommonTrace\n\n"
            "Before starting a non-trivial task, check `memory/lessons/` (or the "
            "CommonTrace Hub via `search_traces`) for applicable lessons. After "
            "finishing, capture what happened with `commontrace capture`. "
            "Spec: protocol/PROTOCOL.md.\n",
        )

    elif args.target == "generic-mcp":
        example = os.path.join(dest, "commontrace.hub.mcp.json.example")
        _write(example, _hub_mcp_example())
        print("  Any MCP-capable agent (OpenAI Agents SDK, custom orchestrators, etc.)")
        print(f"  can attach to the Hub by merging {example} into its MCP client config.")
        _print_hub_credential_warning(example)

    else:  # generic
        out = os.path.join(dest, "COMMONTRACE.md")
        _write(
            out,
            "# CommonTrace\n\n"
            "This project uses the CommonTrace Protocol. See protocol/PROTOCOL.md "
            "for the spec, memory/ for the local store, and `commontrace --help` "
            "for the CLI.\n",
        )

    print("[commontrace] install complete.")
    return 0
