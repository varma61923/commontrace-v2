"""Related CLI operations with lazy, operation-local dependencies."""

from __future__ import annotations

import argparse


# codegraph operations
def codegraph_add_parser(subparsers):
    parser = subparsers.add_parser(
        "codegraph", help="Parse a Python/TypeScript file into source-bound graph relationships."
    )
    parser.add_argument("file")
    parser.add_argument("--project", default="")
    parser.add_argument("--context", action="append", required=True)
    parser.add_argument("--dest")
    parser.set_defaults(func=codegraph_run)


def codegraph_run(args):
    import json

    from commontrace import code_graph, paths

    print(
        json.dumps(
            code_graph.ingest_file(
                paths.resolve_root(args.dest), args.file, project=args.project, context=args.context
            ),
            indent=2,
        )
    )
    return 0


# migrate operations
def migrate_add_parser(subparsers):
    from commontrace import migration

    parser = subparsers.add_parser(
        "migrate",
        help="Import a Mem0, Letta, Zep, Graphiti, Cognee, Supermemory, Hindsight, or EverOS JSON export safely.",
    )
    parser.add_argument("vendor", choices=(*migration.VENDORS, "config"))
    parser.add_argument("file", nargs="?")
    parser.add_argument("--context", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dest")
    parser.set_defaults(func=migrate_run)


def migrate_run(args):
    import json

    from commontrace import migration, paths
    from commontrace.ingest import _open_regular

    if args.vendor == "config":
        from commontrace.configuration import migrate_environment

        print(json.dumps(migrate_environment(paths.resolve_root(args.dest), write=not args.dry_run), indent=2))
        return 0
    if not args.file or not args.context:
        raise ValueError("vendor imports require a file and explicit --context")
    with _open_regular(args.file, 32 * 1024 * 1024) as stream:
        document = json.loads(stream.read(32 * 1024 * 1024 + 1))
    print(
        json.dumps(
            migration.migrate(
                paths.resolve_root(args.dest), args.vendor, document, context=args.context, dry_run=args.dry_run
            ),
            indent=2,
        )
    )
    return 0


# connect operations
def connect_add_parser(subparsers):
    from commontrace.connectors import knowledge_streams
    from commontrace.connectors.knowledge import PROVIDERS

    parser = subparsers.add_parser("connect", help="Read knowledge from provider APIs into a pinned memory scope.")
    parser.add_argument("provider", choices=sorted([*PROVIDERS, *knowledge_streams.STREAMS]))
    parser.add_argument("resource")
    parser.add_argument("--token-env", required=True, help="Environment variable containing the provider OAuth token.")
    parser.add_argument("--context", action="append", required=True)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--account", default="", help="Connection/account name (required for streams).")
    parser.add_argument("--ref", default="HEAD", help="GitHub repository commit or reference.")
    parser.add_argument("--watch", action="store_true", help="Poll completed syncs until interrupted.")
    parser.add_argument("--interval", type=float, default=60, help="Polling interval, 1-86400 seconds.")
    parser.add_argument("--webhook-secret-env", help="Configure the local GitHub push webhook with this secret name.")
    parser.add_argument("--dest")
    parser.set_defaults(func=connect_run)


def connect_run(args):
    import json
    import math
    import sys
    import threading

    from commontrace import paths
    from commontrace.connectors import knowledge_streams
    from commontrace.connectors.knowledge import sync
    from commontrace.secrets_provider import env_secret

    try:
        if not math.isfinite(args.interval) or not 1 <= args.interval <= 86400:
            raise ValueError("polling interval must be in 1..86400")
        root = paths.resolve_root(args.dest)
        if args.webhook_secret_env:
            if args.provider != "github-repo":
                raise ValueError("webhooks require github-repo")
            knowledge_streams.configure_github_webhook(
                root,
                args.resource,
                token_env=args.token_env,
                secret_env=args.webhook_secret_env,
                context=args.context,
                account=args.account,
            )
        stop = threading.Event()
        while True:
            token = env_secret(args.token_env)
            result = (
                knowledge_streams.sync(
                    root,
                    args.provider,
                    args.resource,
                    token=token,
                    context=args.context,
                    max_pages=args.max_pages,
                    account=args.account,
                    ref=args.ref,
                )
                if args.provider in knowledge_streams.STREAMS
                else sync(
                    root, args.provider, args.resource, token=token, context=args.context, max_pages=args.max_pages
                )
            )
            print(json.dumps(result, indent=2), flush=True)
            if not args.watch or stop.wait(args.interval):
                return 0
    except (ValueError, OSError, PermissionError):
        print("Provider sync failed; its watermark was not advanced.", file=sys.stderr)
        return 1


# sql_query operations
def sql_query_add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "sql_query",
        help="Safely execute guarded read-only SELECT queries on SQLite databases.",
        description="Run guarded SQL queries with forbidden keyword validation and row limits.",
    )
    parser.add_argument("db_path", help="Path to SQLite database file.")
    parser.add_argument("sql", help="SQL SELECT query to execute.")
    parser.add_argument("--max-rows", type=int, default=100, help="Max rows to return.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Query timeout in seconds.")
    parser.add_argument("--json", action="store_true", help="Output results as formatted JSON.")
    parser.set_defaults(func=sql_query_run)


def sql_query_run(args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import sql_guard

    try:
        result = sql_guard.execute_guarded_sql(
            args.db_path,
            args.sql,
            max_rows=args.max_rows,
            timeout_seconds=args.timeout,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"Executed: {result['sql']} ({result['row_count']} rows in {result['execution_time_ms']}ms)")
    if result["columns"]:
        print(" | ".join(result["columns"]))
        print("-" * 40)
        for row in result["rows"]:
            print(" | ".join(str(row.get(col, "")) for col in result["columns"]))
    return 0


# viz operations
def viz_add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "viz",
        help="Render the temporal knowledge graph as a self-contained "
        "interactive HTML page (offline force-directed layout).",
    )
    p.add_argument("--as-of", default=None, help="Point-in-time filter (valid_from <= as_of < valid_until).")
    p.add_argument("--out", default=None, help="Output HTML file (default: <store>/graph.html).")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=viz_run)


def viz_run(args: argparse.Namespace) -> int:
    import os
    import sys

    from commontrace import graph_viz, paths

    root = paths.resolve_root(args.dest)
    try:
        html = graph_viz.render_html(root, as_of=args.as_of or None)
    except ValueError as exc:
        print(f"[commontrace] viz: {exc}", file=sys.stderr)
        return 2
    out = args.out or os.path.join(paths.memory_dir(root), "graph.html")
    try:
        out = paths.safe_prepare_output_path(out)
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(html)
    except (OSError, ValueError) as exc:
        print(f"[commontrace] viz: could not write {out!r}: {exc}", file=sys.stderr)
        return 1
    print(f"[commontrace] viz: wrote {out}")
    return 0


# gateway operations
def gateway_add_parser(subparsers: argparse._SubParsersAction) -> None:
    from commontrace import gateway

    p = subparsers.add_parser(
        "gateway",
        help="Serve the causal loop over HTTP+JSON (or JSON lines on stdio) to any agent, "
        "in any language -- an LLM, a ROS node, a C++ planner. Also serves the local console.",
        description=gateway.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dest", default=None, help="Store root (default: auto-detect / $COMMONTRACE_ROOT)")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default: loopback).")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument(
        "--stdio", action="store_true", help="Serve one JSON object per line on stdin/stdout instead of HTTP."
    )
    p.add_argument("--token", default=None, help="Bearer token (default: one generated into memory/gateway.token).")
    token_action = p.add_mutually_exclusive_group()
    token_action.add_argument(
        "--rotate-token",
        action="store_true",
        help="Atomically replace the file-backed token and exit; running gateways reload it.",
    )
    token_action.add_argument(
        "--revoke-token",
        action="store_true",
        help="Revoke the file-backed token and exit; running gateways reject it immediately.",
    )
    p.add_argument(
        "--token-ttl",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Lifetime for a newly created or rotated file-backed token; default: no expiry.",
    )
    p.add_argument(
        "--show-token",
        action="store_true",
        help="Print the bearer token in the console URL (otherwise kept out of logs).",
    )
    p.add_argument(
        "--env", default=None, help="The one environment this store measures (sim, real, ...). Never pooled."
    )
    p.add_argument(
        "--protect",
        action="append",
        default=[],
        metavar="PREFIX",
        help="Item ids starting with PREFIX are safety constraints: always delivered, "
        "never withheld, withdrawn or counted. Repeatable; stored with the store.",
    )
    p.add_argument(
        "--relaxed-durability",
        action="store_true",
        help="Skip fsync on each log line (control loops on flash). A power loss can drop the last few lines.",
    )
    p.add_argument(
        "--on-harm",
        choices=("inform", "withdraw"),
        default=None,
        help="Override the store's harm policy for this gateway.",
    )
    p.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="NAME",
        help="Extra Host header names to answer to (needed when binding beyond loopback).",
    )
    p.add_argument(
        "--allow-approval",
        action="store_true",
        help="Let the console edit, approve and reject lessons in review (off by default: approving "
        "changes what every agent is told).",
    )
    p.add_argument(
        "--allow-self-signup",
        action="store_true",
        help="Allow rate-limited public agent enrollment into isolated scopes; off by default.",
    )
    p.add_argument("--tls-cert", default=None)
    p.add_argument("--tls-key", default=None)
    p.set_defaults(func=gateway_run)


def gateway_run(args: argparse.Namespace) -> int:
    import sys

    from commontrace import gateway, gateway_tokens, paths

    root = paths.resolve_root(args.dest)
    try:
        if args.token and (args.rotate_token or args.revoke_token or args.token_ttl is not None):
            raise ValueError("token rotation, revocation and ttl apply to file-backed tokens, not --token")
        if args.rotate_token:
            token = gateway_tokens.rotate_token(root, ttl=args.token_ttl)
            print(f"[commontrace] gateway token rotated in {gateway.token_path(root)}", file=sys.stderr)
            if args.show_token:
                print(token)
            return 0
        if args.revoke_token:
            gateway_tokens.revoke_token(root)
            print("[commontrace] gateway token revoked", file=sys.stderr)
            return 0
        config = gateway.merge_config(root, env=args.env, protect=args.protect)
        token = None if args.stdio else (args.token or gateway.load_or_create_token(root, ttl=args.token_ttl))
    except (ValueError, OSError) as exc:
        print(f"[commontrace] error: {exc}", file=sys.stderr)
        return 2
    gw = gateway.Gateway(
        root,
        token=token,
        config=config,
        durable=not args.relaxed_durability,
        on_harm=args.on_harm,
        allowed_hosts=tuple(args.allow_host),
        allow_approval=args.allow_approval,
        allow_self_signup=getattr(args, "allow_self_signup", False),
        token_provider=gateway_tokens.FileTokenProvider(root) if not args.stdio and not args.token else None,
    )
    if args.stdio:
        print(f"[commontrace] gateway on stdio for {root}", file=sys.stderr)
        return gateway.serve_stdio(gw, sys.stdin, sys.stdout)

    if bool(args.tls_cert) != bool(args.tls_key):
        print("[commontrace] error: --tls-cert and --tls-key go together", file=sys.stderr)
        return 2
    loopback = args.host in ("127.0.0.1", "::1", "localhost")
    if not loopback and not args.tls_cert:
        print(
            "[commontrace] warning: binding beyond loopback without TLS sends the bearer token in "
            "the clear; terminate TLS in front of this or pass --tls-cert/--tls-key.",
            file=sys.stderr,
        )
    if not loopback and args.host not in ("0.0.0.0", "::"):  # nosec B104 - a comparison, not a bind
        gw.allowed_hosts = gw.allowed_hosts | {args.host.lower()}
    try:
        server = gateway.make_http_server(
            gw, args.host, args.port, tls=(args.tls_cert, args.tls_key) if args.tls_cert else None
        )
    except OSError as exc:
        print(f"[commontrace] cannot listen on {args.host}:{args.port}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    scheme = "https" if args.tls_cert else "http"
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host  # nosec B104 - display only
    port = server.server_address[1]
    print(
        f"[commontrace] gateway for {root}"
        + (f" (environment: {config.env})" if config.env else "")
        + (f", protecting {', '.join(config.protected_prefixes)}" if config.protected_prefixes else "")
        + ("" if gw.durable else ", relaxed durability"),
        file=sys.stderr,
    )
    print(f"  API:      {scheme}://{shown}:{port}/v1/openapi.json   (Authorization: Bearer <token>)", file=sys.stderr)
    console = f"{scheme}://{shown}:{port}/"
    if args.show_token:
        console += f"#token={gw.token}"
    print(f"  Console:  {console}", file=sys.stderr)
    print(f"  Token in: {gateway.token_path(root)}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# jobs operations
def jobs_add_parser(subparsers: argparse._SubParsersAction) -> None:
    from commontrace import jobs

    p = subparsers.add_parser("jobs", help="Queue slow memory work (ingest, extract, distill, summarize, index, link).")
    sub = p.add_subparsers(dest="subcommand", required=True)

    q = sub.add_parser("add", help="Queue a job.")
    q.add_argument("kind", choices=sorted(jobs.HANDLERS))
    q.add_argument("--payload", default="{}", help='JSON object, e.g. \'{"source": "docs/"}\'.')
    q.add_argument("--dedupe-key", default=None, help="Keep at most one pending job per key.")
    q.add_argument("--delay", type=float, default=0.0, help="Seconds before the job may run.")
    q.add_argument("--max-attempts", type=int, default=3)
    q.add_argument("--priority", type=int, default=0)
    q.set_defaults(func=jobs_run_add)

    q = sub.add_parser("list", help="Recent jobs.")
    q.add_argument("--status", choices=jobs.STATUSES, default=None)
    q.add_argument("--limit", type=int, default=30)
    q.set_defaults(func=jobs_run_list)

    q = sub.add_parser("show", help="One job, with its result or error.")
    q.add_argument("job_id")
    q.set_defaults(func=jobs_run_show)

    q = sub.add_parser("run", help="Process ready jobs now.")
    q.add_argument("--limit", type=int, default=20)
    q.add_argument("--kind", action="append", default=[], choices=sorted(jobs.HANDLERS))
    q.add_argument("--watch", action="store_true", help="Keep polling for new jobs.")
    q.add_argument("--interval", type=float, default=5.0)
    q.set_defaults(func=jobs_run_run)

    q = sub.add_parser("retry", help="Requeue a dead or failed job (every dead job when no id is given).")
    q.add_argument("job_id", nargs="?", default=None)
    q.set_defaults(func=jobs_run_retry)

    q = sub.add_parser("purge", help="Delete finished jobs older than N days.")
    q.add_argument("--days", type=float, default=7.0)
    q.set_defaults(func=jobs_run_purge)

    for name, action in sub.choices.items():
        action.add_argument("--dest", default=None)
        action.add_argument("--json", action="store_true")


def jobs__guard(func):
    import sys

    from commontrace import jobs, paths

    def run(args: argparse.Namespace) -> int:
        try:
            return func(args, paths.resolve_root(args.dest))
        except jobs.JobError as exc:
            print(f"[commontrace] jobs: {exc}", file=sys.stderr)
            return 2

    run.__name__ = func.__name__
    return run


@jobs__guard
def jobs_run_add(args, root) -> int:
    import json

    from commontrace import jobs

    try:
        payload = json.loads(args.payload)
    except ValueError as exc:
        raise jobs.JobError(f"--payload is not JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise jobs.JobError("--payload must be a JSON object")
    job = jobs.enqueue(
        root,
        args.kind,
        payload,
        dedupe_key=args.dedupe_key,
        delay=args.delay,
        max_attempts=args.max_attempts,
        priority=args.priority,
    )
    print(json.dumps(job.to_dict(), indent=2) if args.json else f"queued {job.kind} job {job.id} ({job.status})")
    return 0


@jobs__guard
def jobs_run_list(args, root) -> int:
    import json
    import sys
    import time

    from commontrace import jobs

    rows = jobs.list_jobs(root, args.status, args.limit)
    if args.json:
        print(json.dumps([j.to_dict() for j in rows], indent=2))
        return 0
    for j in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(j.updated))
        print(
            f"{j.id}  {j.kind:10s} {j.status:8s} attempts {j.attempts}/{j.max_attempts}  {when}"
            + (f"  {j.error[:80]}" if j.error and j.status != "done" else "")
        )
    print(f"[commontrace] {jobs.counts(root) or 'no jobs'}", file=sys.stderr)
    return 0


@jobs__guard
def jobs_run_show(args, root) -> int:
    import json

    from commontrace import jobs

    job = jobs.get(root, args.job_id)
    if job is None:
        raise jobs.JobError(f"no job {args.job_id}")
    print(json.dumps(job.to_dict(), indent=2, default=str))
    return 0


@jobs__guard
def jobs_run_run(args, root) -> int:
    import json
    import signal
    import threading

    from commontrace import jobs

    total = {"done": 0, "failed": 0}
    stopping = threading.Event()
    previous = {}
    if args.watch and threading.current_thread() is threading.main_thread():
        # Drain on shutdown: a SIGTERM or Ctrl-C lets the job in hand finish and be
        # recorded, then the worker exits. A hard kill is covered by lease reclaim.
        def _stop(_signum, _frame):
            stopping.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, _stop)
    try:
        while not stopping.is_set():
            out = jobs.run_pending(root, limit=1 if args.watch else args.limit, kinds=args.kind or None)
            total["done"] += out["done"]
            total["failed"] += out["failed"]
            if not args.watch:
                break
            if not out["done"] and not out["failed"]:
                stopping.wait(max(0.5, args.interval))
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    summary = {**total, "pending": jobs.counts(root).get("queued", 0)}
    print(
        json.dumps(summary)
        if args.json
        else f"[commontrace] jobs: {summary['done']} done, {summary['failed']} failed, {summary['pending']} queued"
    )
    return 1 if total["failed"] and not total["done"] else 0


@jobs__guard
def jobs_run_retry(args, root) -> int:
    from commontrace import jobs

    n = jobs.retry(root, args.job_id)
    print(f"[commontrace] requeued {n} job(s)")
    return 0 if n or not args.job_id else 1


@jobs__guard
def jobs_run_purge(args, root) -> int:
    from commontrace import jobs

    print(f"[commontrace] deleted {jobs.purge(root, older_than_days=args.days)} finished job(s)")
    return 0


COMMANDS = {
    "codegraph": codegraph_add_parser,
    "migrate": migrate_add_parser,
    "connect": connect_add_parser,
    "sql_query": sql_query_add_parser,
    "viz": viz_add_parser,
    "gateway": gateway_add_parser,
    "jobs": jobs_add_parser,
}
