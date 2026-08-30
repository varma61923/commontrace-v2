from __future__ import annotations

import argparse
import json
import os
import shutil

from commontrace import paths

TARGETS = ["claude-code", "cursor", "devin", "windsurf", "generic-mcp", "generic"]

# The Hub's full tool surface, advertised in the generated MCP config so a
# reader knows what they are connecting to. Kept in sync with hub/smoke.py's
# EXPECTED_TOOLS by hub/tests/test_install_template_surface.py -- this list
# had drifted to the original six while the Hub grew to eighteen, so a
# customer running `commontrace install` was told the Hub could do a third
# of what it does. Restated here rather than imported because this is the
# CLIENT package: it installs with PyYAML alone, and hub/ needs SQLAlchemy,
# asyncpg and a database.
_HUB_TOOLS = [
    # the six protocol tools
    "search_traces", "contribute_trace", "get_trace", "vote_trace", "amend_trace", "list_tags",
    # measurement: is this working, and did the memory cause it
    "fleet_outcomes", "holdout_assign", "record_occasion_outcome",
    # entitlements
    "account_usage",
    # self-service deletion
    "delete_trace", "request_account_deletion", "confirm_account_deletion",
    "cancel_account_deletion",
    # the optional Knowledge Base (absent when HUB_COMMONS_ENABLED=false)
    "commons_overlap", "commons_search", "submit_kb_entry", "list_my_kb_submissions",
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

## Measuring whether any of this helps

The pipeline above is worth nothing if the memory does not improve
outcomes, and only a randomized holdout can establish that it does --
correlational signals (retrieval counts, votes) cannot distinguish a
lesson that helps from one that merely fires on hard tasks.

Both tiers support it, and in both the agent's job is the same: honour
what is withheld, and report how the task went.

**Hub tier.** Pass your own `occasion_id` to `search_traces`. If an
operator has started an experiment, the response carries a `holdout`
block:

    search_traces(query="...", occasion_id="ticket-8821")
      -> {"traces": [...],
          "holdout": {"withhold": ["<trace-id>", ...]}}

**Do not use any trace listed under `holdout.withhold` on that occasion.**
Using one anyway does not raise an error -- it silently moves the occasion
into the treated arm and biases the measured effect toward zero. Then,
when the task finishes:

    record_occasion_outcome(occasion_id="ticket-8821", succeeded=true)

With no experiment running, no `holdout` block appears and nothing
changes.

**Local tier.** `commontrace query --experiment --occasion-id <id>` does
the withholding and logging in one step; `commontrace capture
--occasion-id <id>` joins the outcome back to it.

Read the result with `commontrace prove outcomes` (Hub) or
`commontrace experiment` (local). A lesson can come back as `HURTS`; that
is the point.
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


def _break_symlink(path: str) -> None:
    """Replace a symlink at `path` with a normal file, before writing it.

    Both `open(path, "w")` and `shutil.copyfile` FOLLOW a symlink and write
    through to whatever it points at. `install` writes to fixed, predictable
    locations inside someone else's workspace (`.claude/skills/commontrace/
    SKILL.md`, `.cursor/rules/commontrace.mdc`), so a symlink planted at one
    of those paths -- or left there by an earlier dotfile-manager setup that
    links config into a repo -- silently redirects the write to an arbitrary
    file. Reproduced: with SKILL.md symlinked to a file outside the project,
    `install` overwrote that file's contents and left the symlink in place,
    so nothing in the output revealed what had happened.

    `os.path.isfile` does not help here -- it follows the link too, and
    returns True for a symlink to a regular file. `os.path.islink` is the
    only check that sees the link itself.
    """
    if os.path.islink(path):
        print(f"  [WARN] replacing symlink (not writing through it): {path}")
        os.unlink(path)


def _write(path: str, content: str) -> None:
    _break_symlink(path)
    if os.path.isfile(path):
        print(f"  [WARN] overwriting existing file: {path}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    print(f"  wrote {path}")


def _copy(src: str, dest: str) -> None:
    _break_symlink(dest)
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
