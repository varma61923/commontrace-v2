from __future__ import annotations

import argparse

_MESSAGE = """\
[commontrace] `sync` is a bridge to the CommonTrace Hub, not a standalone
network client: the Hub is reached over MCP (search_traces, contribute_trace,
get_trace, vote_trace, amend_trace, list_tags), which is an agent-side
transport, not something a bare pip package can dial on its own without your
org's Hub credentials/endpoint.

What to actually do:
  - An MCP-capable agent (Claude Code, Cursor, Devin, ...) with the
    "commontrace" MCP server attached (see `commontrace install --target
    generic-mcp`) can push a local lesson to the Hub itself by calling
    contribute_trace(title=lesson.description, context_text=applies_when,
    solution_text="Rule + How to apply", tags=lesson.tags), then recording
    the returned trace id back into the lesson's `hub_trace_id` field.
  - Pulling: call search_traces(query=..., tags=[...]) and land results in
    memory/traces/ as candidate Traces awaiting `commontrace lesson new`
    promotion.

See protocol/PROTOCOL.md#5-store-two-conformance-tiers for the full mapping.
"""


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "sync",
        help="Explain how to bridge the local store to the CommonTrace Hub (MCP-mediated).",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    print(_MESSAGE)
    return 0
