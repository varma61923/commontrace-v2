"""`commontrace serve` -- the local store, over MCP stdio.

The command exists so an agent can be pointed at its own memory with a config
entry rather than a shell. See commontrace/mcp_server.py for why that matters.
"""
from __future__ import annotations

import argparse
import sys

from commontrace import paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "serve",
        help="Serve this store to an agent over MCP (stdio).",
        description=(
            "Expose the local store as an MCP server on stdin/stdout, so an agent "
            "with no terminal can retrieve, capture, and curate its own memory. "
            "Run by the MCP client, not by hand -- `commontrace install --mcp` "
            "writes the config entry that launches it."
        ),
    )
    p.add_argument("--dest", default=None,
                   help="Store root (default: auto-detect / $COMMONTRACE_ROOT)")
    p.add_argument(
        "--no-approval", dest="allow_approval", action="store_false", default=True,
        help="Omit approve_lesson/reject_lesson entirely, for a deployment where "
             "activating a lesson must go through a person. The tools are absent "
             "from the listing, not merely refused, so the agent never plans around "
             "a call it cannot make.",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    # stderr, never stdout: stdout is the MCP wire (mcp_server.py).
    print(f"[commontrace] serving {root} over MCP stdio"
          + ("" if args.allow_approval else " (approval tools disabled)"),
          file=sys.stderr)
    from commontrace import mcp_server

    try:
        return mcp_server.serve(root, allow_approval=args.allow_approval)
    except mcp_server.LocalStoreError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
