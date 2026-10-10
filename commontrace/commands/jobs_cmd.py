"""`commontrace jobs`: the durable queue for slow memory work."""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time

from commontrace import jobs, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("jobs", help="Queue slow memory work (ingest, extract, distill, summarize, index, link).")
    sub = p.add_subparsers(dest="subcommand", required=True)

    q = sub.add_parser("add", help="Queue a job.")
    q.add_argument("kind", choices=sorted(jobs.HANDLERS))
    q.add_argument("--payload", default="{}", help="JSON object, e.g. '{\"source\": \"docs/\"}'.")
    q.add_argument("--dedupe-key", default=None, help="Keep at most one pending job per key.")
    q.add_argument("--delay", type=float, default=0.0, help="Seconds before the job may run.")
    q.add_argument("--max-attempts", type=int, default=3)
    q.add_argument("--priority", type=int, default=0)
    q.set_defaults(func=run_add)

    q = sub.add_parser("list", help="Recent jobs.")
    q.add_argument("--status", choices=jobs.STATUSES, default=None)
    q.add_argument("--limit", type=int, default=30)
    q.set_defaults(func=run_list)

    q = sub.add_parser("show", help="One job, with its result or error.")
    q.add_argument("job_id")
    q.set_defaults(func=run_show)

    q = sub.add_parser("run", help="Process ready jobs now.")
    q.add_argument("--limit", type=int, default=20)
    q.add_argument("--kind", action="append", default=[], choices=sorted(jobs.HANDLERS))
    q.add_argument("--watch", action="store_true", help="Keep polling for new jobs.")
    q.add_argument("--interval", type=float, default=5.0)
    q.set_defaults(func=run_run)

    q = sub.add_parser("retry", help="Requeue a dead or failed job (every dead job when no id is given).")
    q.add_argument("job_id", nargs="?", default=None)
    q.set_defaults(func=run_retry)

    q = sub.add_parser("purge", help="Delete finished jobs older than N days.")
    q.add_argument("--days", type=float, default=7.0)
    q.set_defaults(func=run_purge)

    for name, action in sub.choices.items():
        action.add_argument("--dest", default=None)
        action.add_argument("--json", action="store_true")


def _guard(func):
    def run(args: argparse.Namespace) -> int:
        try:
            return func(args, paths.resolve_root(args.dest))
        except jobs.JobError as exc:
            print(f"[commontrace] jobs: {exc}", file=sys.stderr)
            return 2
    run.__name__ = func.__name__
    return run


@_guard
def run_add(args, root) -> int:
    try:
        payload = json.loads(args.payload)
    except ValueError as exc:
        raise jobs.JobError(f"--payload is not JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise jobs.JobError("--payload must be a JSON object")
    job = jobs.enqueue(root, args.kind, payload, dedupe_key=args.dedupe_key, delay=args.delay,
                       max_attempts=args.max_attempts, priority=args.priority)
    print(json.dumps(job.to_dict(), indent=2) if args.json else f"queued {job.kind} job {job.id} ({job.status})")
    return 0


@_guard
def run_list(args, root) -> int:
    rows = jobs.list_jobs(root, args.status, args.limit)
    if args.json:
        print(json.dumps([j.to_dict() for j in rows], indent=2))
        return 0
    for j in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(j.updated))
        print(f"{j.id}  {j.kind:10s} {j.status:8s} attempts {j.attempts}/{j.max_attempts}  {when}"
              + (f"  {j.error[:80]}" if j.error and j.status != "done" else ""))
    print(f"[commontrace] {jobs.counts(root) or 'no jobs'}", file=sys.stderr)
    return 0


@_guard
def run_show(args, root) -> int:
    job = jobs.get(root, args.job_id)
    if job is None:
        raise jobs.JobError(f"no job {args.job_id}")
    print(json.dumps(job.to_dict(), indent=2, default=str))
    return 0


@_guard
def run_run(args, root) -> int:
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
    print(json.dumps(summary) if args.json else
          f"[commontrace] jobs: {summary['done']} done, {summary['failed']} failed, {summary['pending']} queued")
    return 1 if total["failed"] and not total["done"] else 0


@_guard
def run_retry(args, root) -> int:
    n = jobs.retry(root, args.job_id)
    print(f"[commontrace] requeued {n} job(s)")
    return 0 if n or not args.job_id else 1


@_guard
def run_purge(args, root) -> int:
    print(f"[commontrace] deleted {jobs.purge(root, older_than_days=args.days)} finished job(s)")
    return 0
