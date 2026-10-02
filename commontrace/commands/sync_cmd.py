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
     `--push-traces` additionally pushes captured traces (including any
     `commontrace capture --resolved/--tokens-used/...` outcome data) --
     not run by default, since a raw trace is a specific incident record
     rather than curated knowledge and pushing it should be a deliberate
     choice.

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
    p.add_argument(
        "--push-traces", action="store_true",
        help="Push captured traces (commontrace capture), including any recorded outcome "
             "data, to the Hub via contribute_trace/amend_trace. Independent of --push/--pull "
             "and NOT run by default -- a raw trace is a specific incident record, not curated "
             "knowledge, so pushing it is opt-in.",
    )
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
    p.add_argument("--connector", default=None,
                   help="Auto-sync connector to run (local_dir, web_crawler). "
                        "When given, Hub push/pull is skipped and the connector "
                        "syncs into the local store instead.")
    p.add_argument("--source", action="append", default=None,
                   help="Connector source: directory path (local_dir) or URL "
                        "(web_crawler). Repeatable; comma-separated also accepted.")
    p.add_argument("--scope", default="",
                   help="Routing scope for connector facts (e.g. payments).")
    p.add_argument("--dry-run", action="store_true",
                   help="Connector dry-run: fetch/chunk with zero writes "
                        "(no facts, provenance, or state).")
    p.add_argument("--run-id", default="",
                   help="Provenance run identifier (default: generated).")
    p.add_argument("--state-token", default=None,
                   help="Explicit resume token (default: persisted state).")
    p.add_argument("--max-files", type=int, default=200,
                   help="Max changed files per local_dir sync.")
    p.add_argument("--max-pages", type=int, default=20,
                   help="Max pages per web_crawler sync.")
    p.add_argument("--timeout", type=int, default=10,
                   help="Fetch timeout (seconds) for web_crawler.")
    p.add_argument(
        "--fail-if-unconfigured", action="store_true",
        help="Exit 2 (instead of 0) when no Hub is configured. For scripts "
             "that must distinguish 'synced' from 'nothing configured'. "
             "Also enabled by COMMONTRACE_SYNC_STRICT=1.",
    )
    p.add_argument(
        "--quiet", action="store_true",
        help="Suppress the unconfigured-Hub help text on stdout (the stderr "
             "marker is still printed). For cron wrappers.",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if getattr(args, "connector", None):
        return _run_connector(args)
    hub_url = args.hub_url or os.environ.get("COMMONTRACE_HUB_URL")
    hub_api_key = args.hub_api_key or os.environ.get("COMMONTRACE_HUB_API_KEY")

    if args.hub_api_key:
        print(
            "[commontrace] [WARN] --hub-api-key was passed on the command line, which is "
            "visible to other local users (`ps`, /proc, shell history). Prefer setting "
            "COMMONTRACE_HUB_API_KEY instead.",
            file=sys.stderr,
        )

    if not hub_url or not hub_api_key:
        if not getattr(args, "quiet", False):
            print(_MESSAGE)
        print(
            "[commontrace] sync: no Hub configured (COMMONTRACE_HUB_URL/API_KEY); "
            "nothing pushed or pulled (exit 0 preserved for compatibility; "
            "pass --fail-if-unconfigured for exit 2).",
            file=sys.stderr,
        )
        env_strict = os.environ.get("COMMONTRACE_SYNC_STRICT", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        strict = getattr(args, "fail_if_unconfigured", False) or env_strict
        return 2 if strict else 0

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

        if args.push_traces:
            trace_results = asyncio.run(hub_client.push_captured_traces(hub_url, hub_api_key, root))
            n_ok = sum(1 for r in trace_results if r.hub_trace_id and not r.error and not r.skipped)
            n_err = sum(1 for r in trace_results if r.error)
            n_skip = sum(1 for r in trace_results if r.skipped)
            print(
                f"[commontrace] sync --push-traces: {n_ok} trace(s) pushed, {n_skip} already on the "
                f"Hub, {n_err} error(s), out of {len(trace_results)}."
            )
            for r in trace_results:
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


def _flatten_sources(raw: object) -> list[str]:
    out: list[str] = []
    items = raw if isinstance(raw, list) else ([raw] if raw else [])
    for item in items:
        for part in str(item).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _run_connector(args: argparse.Namespace) -> int:
    root = paths.resolve_root(getattr(args, "dest", None))
    name = str(getattr(args, "connector", "") or "").strip()
    sources = _flatten_sources(getattr(args, "source", None))
    scope = getattr(args, "scope", "") or ""
    dry_run = bool(getattr(args, "dry_run", False))
    run_id = getattr(args, "run_id", "") or ""
    state_token = getattr(args, "state_token", None)
    max_files = getattr(args, "max_files", 200) or 200
    max_pages = getattr(args, "max_pages", 20) or 20
    timeout = getattr(args, "timeout", 10) or 10

    try:
        if name == "local_dir":
            from commontrace.connectors.local_dir import LocalDirConnector

            if not sources:
                print("[commontrace] sync --connector local_dir requires --source <dir>.",
                      file=sys.stderr)
                return 2
            connector = LocalDirConnector()
            sync_result = connector.sync(
                root, state_token, scope=scope, run_id=run_id,
                dry_run=dry_run, source_dir=sources[0], max_files=max_files,
            )
        elif name == "web_crawler":
            from commontrace.connectors.web_crawler import WebCrawlerConnector

            if not sources:
                print("[commontrace] sync --connector web_crawler requires --source <url>.",
                      file=sys.stderr)
                return 2
            connector = WebCrawlerConnector()
            sync_result = connector.sync(
                root, state_token, scope=scope, run_id=run_id,
                dry_run=dry_run, urls=sources, max_pages=max_pages,
                timeout=timeout,
            )
        else:
            print(f"[commontrace] sync: unknown connector {name!r} "
                  "(expected 'local_dir' or 'web_crawler').", file=sys.stderr)
            return 2
    except Exception as exc:
        print(f"[commontrace] sync --connector {name} failed: {exc}", file=sys.stderr)
        return 1

    d = sync_result.result.to_dict() if sync_result.result is not None else {}
    mode = "dry-run " if dry_run else ""
    print(f"[commontrace] sync --connector {name}: {mode}{d.get('chunks_extracted', 0)} "
          f"chunk(s), {d.get('facts_written', 0)} fact(s), "
          f"{len(sync_result.result.errors) if sync_result.result else 0} error(s).")
    if sync_result.pending:
        print(f"  {sync_result.pending} more item(s) are waiting; run the sync again to continue.")
    print(f"  run_id={sync_result.run_id}")
    print(f"  new_state_token={sync_result.new_state_token}")
    for err in (sync_result.result.errors if sync_result.result else []):
        print(f"  [ERROR] {err}", file=sys.stderr)
    if dry_run:
        print("  dry-run: no facts, provenance, or state were written.")
    return 1 if sync_result.result and sync_result.result.errors else 0
