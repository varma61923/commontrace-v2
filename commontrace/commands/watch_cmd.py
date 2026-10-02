"""`commontrace watch`: single-pass cascade scan + lesson-cache reconcile."""
from __future__ import annotations

import argparse

from commontrace import paths
from commontrace import watch as watch_mod


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "watch",
        help="Single-pass scan of memory/lessons + memory/graph; rebuild the "
             "lesson cache when anything changed. Re-run via cron/systemd.",
    )
    p.add_argument("--once", action="store_true",
                   help="Run one pass and exit (the only mode; kept for "
                        "forward-compat with a future long-running watcher).")
    p.add_argument("--state-file", default=None,
                   help="Path to the watch state JSON (default: memory/.cache/watch_state.json).")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    result = watch_mod.reconcile(root, getattr(args, "state_file", None))
    changed = result.get("changed", [])
    if not changed:
        print("[commontrace] watch: no changes.")
        return 0
    print(f"[commontrace] watch: {len(changed)} changed file(s)"
          + (", cache rebuilt." if result.get("rebuilt") else "."))
    for rel in changed:
        print(f"  changed: {rel}")
    return 0
