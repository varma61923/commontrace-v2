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
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run one pass and exit (default).")
    mode.add_argument("--daemon", action="store_true", help="Watch until interrupted; debounce direct Markdown edits.")
    p.add_argument("--debounce", type=float, default=.5, help="Quiet period in seconds (daemon only).")
    p.add_argument("--state-file", default=None,
                   help="Path to the watch state JSON (default: memory/.cache/watch_state.json).")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    if getattr(args, "daemon", False):
        watch_mod.run_forever(root, state_file=args.state_file, debounce=args.debounce)
        return 0
    result = watch_mod.reconcile(root, getattr(args, "state_file", None))
    if not result.get("cache", {}).get("ok"):
        print("[commontrace] watch: rebuild failed; watermark retained for retry.")
        return 1
    changed = result.get("changed", [])
    if not changed:
        print("[commontrace] watch: no changes.")
        return 0
    print(f"[commontrace] watch: {len(changed)} changed file(s)"
          + (", cache rebuilt." if result.get("rebuilt") else "."))
    for rel in changed:
        print(f"  changed: {rel}")
    return 0
