"""`commontrace daemon`: one guarded consolidation pass (cron/idle/event)."""
from __future__ import annotations

import argparse
import json

from commontrace import daemon as daemon_mod
from commontrace import paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "daemon",
        help="Run one guarded consolidation pass (file lock + crash marker; "
             "calls the dream/consolidate entry points). Schedule via cron/systemd.",
    )
    p.add_argument("--once", action="store_true",
                   help="Run one pass and exit (the only mode).")
    p.add_argument("--trigger", default="once",
                   choices=("once", "cron", "manual", "event", "idle"),
                   help="Which trigger semantics gate this pass.")
    p.add_argument("--json", action="store_true",
                   help="Print the pass status dict as JSON.")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    result = daemon_mod.run_once(root, getattr(args, "trigger", "once"))
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    elif result.get("ran"):
        flag = " (recovered from previous crash)" if result.get("recovered") else ""
        print(f"[commontrace] daemon: pass complete{flag}.")
    elif result.get("reason") == "locked":
        print("[commontrace] daemon: another pass holds the lock; skipping.")
        return 1
    else:
        print(f"[commontrace] daemon: skipped ({result.get('reason', 'no trigger')}).")
    return 0 if result.get("ok") else 1
