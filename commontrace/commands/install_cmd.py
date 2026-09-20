from __future__ import annotations

import argparse
import json
import os
import shutil

from commontrace import mcp_tools, paths

TARGETS = ["claude-code", "cursor", "devin", "windsurf", "generic-mcp", "generic"]

# The Hub's full tool surface, advertised in the generated MCP config so a
# reader knows what they are connecting to. Kept in sync with hub/smoke.py's
# EXPECTED_TOOLS by hub/tests/test_install_template_surface.py -- this list
# had drifted to the original six while the Hub kept growing, so a
# customer running `commontrace install` was told the Hub could do a third
# of what it does. Restated here rather than imported because this is the
# CLIENT package: it installs with PyYAML alone, and hub/ needs SQLAlchemy,
# asyncpg and a database.
_HUB_TOOLS = [
    # the six protocol tools
    "search_traces", "contribute_trace", "get_trace", "vote_trace", "amend_trace", "list_tags",
    # measurement: is this working, did the memory cause it, and what was
    # that worth (the last one is the pricing basis, STRATEGY.md 11.5)
    "fleet_outcomes", "holdout_assign", "record_occasion_outcome",
    "value_delivered",
    # the graduated subset of that measurement: the memories whose effect
    # is already established, rendered once per session as a pinnable
    # block instead of paid for on every query
    "working_set",
    # entitlements
    "account_usage",
    # self-service deletion
    "delete_trace", "request_account_deletion", "confirm_account_deletion",
    "cancel_account_deletion",
    # the optional Knowledge Base (absent when HUB_COMMONS_ENABLED=false)
    "commons_overlap", "commons_search", "submit_kb_entry", "list_my_kb_submissions",
    # collaboration on a trace for a customer's own team: comments,
    # assignment, and a per-person notification inbox
    "add_comment", "list_comments", "assign_trace", "unassign_trace",
    "list_my_notifications", "mark_notification_read",
    # locating traces for a subject-erasure request
    "search_trace_content",
    # structured subject tagging + exact-match find/purge, the other half
    # of subject-erasure support
    "tag_trace_subjects", "find_traces_by_subject", "purge_traces_by_subject",
]

_GENERIC_POINTER_SKILL = """---
name: commontrace
description: "Pointer skill — no reference pipeline (SKILL.md) was found on disk. \
Read protocol/PROTOCOL.md for the CommonTrace Protocol spec and implement \
Capture → Structure → Extract → Validate → Store → Inject → Measure for this agent."
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

**Local tier.** This tier speaks MCP too, so a shell is not required:
if the `commontrace-local` server is attached (see
`commontrace.local.mcp.json`, written by `commontrace install`), call

    retrieve(task="...", occasion_id="ticket-8821")
      -> {"lessons": [...], "withheld": [...]}

and honour `withheld` exactly as you honour the Hub's `holdout.withhold`
above, then `capture(occasion_id="ticket-8821", resolved=true)`.

From a shell the same thing is `commontrace query --experiment
--occasion-id <id>` and `commontrace capture --occasion-id <id>`. Both
surfaces share one arm-assignment implementation, so an occasion gets the
same arm whichever one you use.

Report an outcome for EVERY occasion you retrieved against, including
the ones that were abandoned or escalated. Skipping the ones that went
badly is the failure that biases the result most: the withheld arm is
the one working without its memory, so it is the arm that runs long and
gets abandoned. Both readers audit for this and refuse to report an
effect when they find it, so an incomplete write-up produces no number
rather than a wrong one.

Read the result with `commontrace prove outcomes` (Hub) or
`commontrace experiment` (local). A lesson can come back as `HURTS`;
that is the point -- provided the validity section above it says the run
is sound.
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


def _local_mcp_config(root: str) -> str:
    """The stdio MCP entry that attaches an agent to THIS machine's store.

    Distinct from _hub_mcp_example above, and both are usually wanted: the Hub
    is the shared knowledge base over HTTP, this is the fleet's own memory on
    this machine over stdio. An agent with only the Hub entry can search what
    other people published and cannot read or write a single one of its own
    lessons.

    Written with a real, absolute `--dest`, not a relative one: an MCP client
    launches the server as a subprocess with a working directory of its own
    choosing, so a relative root resolves somewhere else -- usually to a new,
    empty store, which fails by silently having no lessons rather than by
    erroring.
    """
    tools = ", ".join(mcp_tools.LOCAL_TOOLS)
    doc = {
        "_comment": (
            "Merge the 'commontrace-local' entry into your agent platform's MCP config. "
            f"Tool surface: [{tools}]. This attaches the agent to the store at "
            f"{root} on this machine, over stdio -- there is no network listener, no "
            "endpoint and no credential, so unlike the Hub template this file is safe to "
            "commit. Add --no-approval to the args if activating a lesson must go "
            "through a person."
        ),
        "mcpServers": {
            "commontrace-local": {
                "command": "commontrace",
                "args": ["serve", "--dest", root],
            }
        },
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _write_local_mcp(dest: str, root: str) -> str:
    path = os.path.join(dest, "commontrace.local.mcp.json")
    _write(path, _local_mcp_config(root))
    print(f"  Agent-native access to this store: merge {path} into your MCP config")
    print("  (that is what lets an agent with no terminal use its own memory).")
    return path


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "install",
        help="Wire a specific agent platform to read/write this store (or the Hub).",
    )
    p.add_argument("--target", choices=TARGETS, required=True)
    p.add_argument("--dest", default=".", help="Project directory to install into (default: current directory)")
    p.set_defaults(func=run)


def _find_skill_md(root: str, dest: str) -> str | None:
    # cwd first, then `root` (paths.resolve_root(), which honors
    # COMMONTRACE_ROOT/JUSTDOIT_ROOT): `install` is normally run from
    # inside the commontrace checkout that actually has SKILL.md, but an
    # operator with COMMONTRACE_ROOT exported to point at their OWN active
    # store (e.g. a wrapper script that always sets it) and running
    # `install --dest /some/other/project` from a plain shell got THAT
    # unrelated store's SKILL.md silently, instead of the one next to the
    # command actually being run.
    candidates = [
        os.path.join(os.getcwd(), "SKILL.md"),
        os.path.join(root, "SKILL.md"),
        os.path.join(dest, "SKILL.md"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _break_symlink(path: str, base_dir: str | None = None) -> None:
    """Replace a symlink at `path` (and any intermediate directory symlinks) with
    a normal file/directory, before writing it.

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
    path_abs = os.path.abspath(path)
    if base_dir is not None:
        base_abs = os.path.abspath(base_dir)
        try:
            rel = os.path.relpath(path_abs, base_abs)
        except ValueError:
            rel = None
        if rel and not rel.startswith(".."):
            parts = rel.split(os.sep)
            curr = base_abs
            for part in parts:
                curr = os.path.join(curr, part)
                if os.path.islink(curr):
                    print(f"  [WARN] replacing symlink (not writing through it): {curr}")
                    os.unlink(curr)
            paths.enforce_boundary(base_abs, path_abs)
            return

    components = []
    head = path_abs
    while True:
        components.append(head)
        parent = os.path.dirname(head)
        if parent == head:
            break
        head = parent
    components.reverse()
    for comp in components:
        if comp == os.sep:
            continue
        if os.path.islink(comp):
            print(f"  [WARN] replacing symlink (not writing through it): {comp}")
            os.unlink(comp)


def _write(path: str, content: str, base_dir: str | None = None) -> None:
    _break_symlink(path, base_dir=base_dir)
    if os.path.isfile(path):
        print(f"  [WARN] overwriting existing file: {path}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if base_dir is not None:
        paths.enforce_boundary(base_dir, path)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    print(f"  wrote {path}")


def _copy(src: str, dest: str, base_dir: str | None = None) -> None:
    _break_symlink(dest, base_dir=base_dir)
    if os.path.isfile(dest):
        print(f"  [WARN] overwriting existing file: {dest}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if base_dir is not None:
        paths.enforce_boundary(base_dir, dest)
    shutil.copyfile(src, dest)
    print(f"  copied {src} -> {dest}")


def _print_hub_credential_warning(example_path: str) -> None:
    print(
        f"  [WARN] {example_path} is a template for Hub connection details. "
        "Once you fill in a real endpoint/API key, make sure that file (or wherever "
        "you merge it, e.g. mcp.json) is covered by .gitignore before committing."
    )


def run(args: argparse.Namespace) -> int:
    dest = os.path.abspath(args.dest)
    # Resolve the commontrace store root from the *destination* directory, not
    # from cwd: when --dest points at a separate checkout or agent home, the
    # generated .mcp.json must reference that store's root path, not the
    # operator's working directory (which leaks host paths and likely points at
    # the wrong store). resolve_root(dest) inspects dest for a memory/ dir and
    # falls back to dest itself -- correct for both in-repo and external installs.
    root = paths.resolve_root(dest)
    skill_md = _find_skill_md(root, dest)
    print(f"[commontrace] installing target='{args.target}' into {dest}")

    if args.target == "claude-code":
        out = os.path.join(dest, ".claude", "skills", "commontrace", "SKILL.md")
        if skill_md:
            _copy(skill_md, out, base_dir=dest)
        else:
            _write(out, _GENERIC_POINTER_SKILL, base_dir=dest)
        print("  Invoke with: /commontrace <task description + success criteria>")
        _write_local_mcp(dest, root)

    elif args.target == "devin":
        out = os.path.join(dest, ".devin", "skills", "commontrace", "SKILL.md")
        if skill_md:
            _copy(skill_md, out, base_dir=dest)
        else:
            _write(out, _GENERIC_POINTER_SKILL, base_dir=dest)

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
            base_dir=dest,
        )
        example = os.path.join(dest, "commontrace.hub.mcp.json.example")
        _write(example, _hub_mcp_example(), base_dir=dest)
        print(f"  To connect to the Hub: merge {example} into .cursor/mcp.json")
        _print_hub_credential_warning(example)
        _write_local_mcp(dest, root)

    elif args.target == "windsurf":
        out = os.path.join(dest, ".windsurf", "rules", "commontrace.md")
        _write(
            out,
            "# CommonTrace\n\n"
            "Before starting a non-trivial task, check `memory/lessons/` (or the "
            "CommonTrace Hub via `search_traces`) for applicable lessons. After "
            "finishing, capture what happened with `commontrace capture`. "
            "Spec: protocol/PROTOCOL.md.\n",
            base_dir=dest,
        )
        _write_local_mcp(dest, root)

    elif args.target == "generic-mcp":
        example = os.path.join(dest, "commontrace.hub.mcp.json.example")
        _write(example, _hub_mcp_example(), base_dir=dest)
        print("  Any MCP-capable agent (OpenAI Agents SDK, custom orchestrators, etc.)")
        print(f"  can attach to the Hub by merging {example} into its MCP client config.")
        _print_hub_credential_warning(example)
        _write_local_mcp(dest, root)

    else:  # generic
        out = os.path.join(dest, "COMMONTRACE.md")
        _write(
            out,
            "# CommonTrace\n\n"
            "This project uses the CommonTrace Protocol. See protocol/PROTOCOL.md "
            "for the spec, memory/ for the local store, and `commontrace --help` "
            "for the CLI.\n",
            base_dir=dest,
        )

    print("[commontrace] install complete.")
    return 0
