from __future__ import annotations

import argparse
import asyncio
import os
import sys

from commontrace import paths

_MESSAGE = """\
[commontrace] `sync` bridges the local store to the CommonTrace Hub over MCP
(search_traces, contribute_trace, get_trace, vote_trace, amend_trace,
list_tags). No Hub is configured for this invocation.

To connect one:
  1. Run a Hub (see hub/README.md) and issue an API key for your org
     (`python -m hub.manage issue-key <org_id>`).
  2. Set COMMONTRACE_HUB_URL (e.g. http://localhost:8420/mcp) and
     COMMONTRACE_HUB_API_KEY, or pass --hub-url/--hub-api-key.
  3. Install the client extra: `pip install commontrace[hub-sync]`.
  4. Re-run `commontrace sync` (pushes active lessons + pulls search
     results by default), or `--push`/`--pull` for just one direction.

An MCP-capable agent (Claude Code, Cursor, Devin, ...) with the
"commontrace" MCP server attached (see `commontrace install --target
generic-mcp`) can also call the Hub tools directly itself, independent of
this CLI command.

See protocol/PROTOCOL.md#5-store-two-conformance-tiers for the full mapping.
"""


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "sync",
        help="Bridge the local store to the CommonTrace Hub (MCP-mediated).",
    )
    p.add_argument("--push", action="store_true", help="Push active lessons to the Hub only.")
    p.add_argument("--pull", action="store_true", help="Pull search_traces results into memory/traces/ only.")
    p.add_argument("--query", default="", help="search_traces query text (--pull only).")
    p.add_argument("--tags", default="", help="Comma-separated search_traces tags filter (--pull only).")
    p.add_argument("--hub-url", default=None, help="Default: $COMMONTRACE_HUB_URL")
    p.add_argument(
        "--hub-api-key",
        default=None,
        help="Hub API key ($COMMONTRACE_HUB_API_KEY environment variable is preferred "
             "to avoid process table exposure).",
    )
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    hub_url = args.hub_url or os.environ.get("COMMONTRACE_HUB_URL")
    hub_api_key = args.hub_api_key or os.environ.get("COMMONTRACE_HUB_API_KEY")

    if args.hub_api_key:
        # A CLI argument is readable by any local user via `ps`/
        # /proc/<pid>/cmdline, and can land in shell history and auditd's
        # process-exec logs -- none of which apply to an environment
        # variable. --help already says the env var is preferred; this is
        # the same warning at the moment it actually matters.
        print(
            "[commontrace] [WARN] --hub-api-key was passed on the command line, which is "
            "visible to other local users (`ps`, /proc, shell history). Prefer setting "
            "COMMONTRACE_HUB_API_KEY instead.",
            file=sys.stderr,
        )

    if not hub_url or not hub_api_key:
        print(_MESSAGE)
        return 0

    from commontrace import hub_client

    root = paths.resolve_root(args.dest)
    do_push = args.push or not args.pull
    do_pull = args.pull or not args.push

    try:
        if do_push:
            results = asyncio.run(hub_client.push_active_lessons(hub_url, hub_api_key, root))
            n_ok = sum(1 for r in results if r.hub_trace_id and not r.error and not r.skipped)
            n_err = sum(1 for r in results if r.error)
            n_skip = sum(1 for r in results if r.skipped)
            print(
                f"[commontrace] sync --push: {n_ok} lesson(s) pushed, {n_skip} already on the Hub, "
                f"{n_err} error(s), out of {len(results)}."
            )
            for r in results:
                if r.error:
                    print(f"  [ERROR] {r.slug}: {r.error}", file=sys.stderr)
                elif r.skipped:
                    print(f"  {r.slug} -> already hub_trace_id={r.hub_trace_id} (unchanged)")
                else:
                    tag = " (quarantined pending review)" if r.quarantined else ""
                    print(f"  {r.slug} -> hub_trace_id={r.hub_trace_id}{tag}")

        if do_pull:
            tags = [t.strip() for t in args.tags.split(",") if t.strip()]
            pull_result = asyncio.run(hub_client.pull_search_results(hub_url, hub_api_key, root, args.query, tags))
            print(
                f"[commontrace] sync --pull: {pull_result.n_found} trace(s) found, "
                f"{len(pull_result.written_paths)} new candidate(s) written to memory/traces/."
            )
            for path in pull_result.written_paths:
                print(f"  wrote {path}")
            if pull_result.ignored_terms:
                # Printed whether or not anything was found: these terms did
                # not participate in the search, so even a non-empty result
                # was matched on less than was asked for.
                print(
                    "  Not searched on: "
                    + ", ".join(pull_result.ignored_terms)
                    + " -- too common in your corpus to tell traces apart."
                )
                if not pull_result.n_found:
                    print("  Try a more specific word; nothing was matched on at all.")
            if pull_result.written_paths:
                print("  Promote a candidate with `commontrace lesson new` once reviewed.")

    except hub_client.HubClientUnavailable as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    except hub_client.HubConnectionError as exc:
        print(f"[commontrace] sync failed: {exc}", file=sys.stderr)
        return 1

    return 0
