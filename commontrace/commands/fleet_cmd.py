"""`commontrace fleet`: many robots, one experiment (see commontrace/fleet.py)."""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import fleet


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "fleet",
        help="Run one experiment across many robots or edge agents: give them the same "
             "randomization, then pool their records.",
    )
    sub = p.add_subparsers(dest="fleet_cmd", required=True)

    ad = sub.add_parser("adopt", help="Copy SOURCE's experiment and gateway policy into a robot's store.")
    ad.add_argument("source", help="The store that started the experiment.")
    ad.add_argument("--dest", required=True, help="The robot's store.")
    ad.set_defaults(func=run_adopt)

    ck = sub.add_parser("check", help="Say whether these stores can be pooled, and what would not.")
    ck.add_argument("stores", nargs="+")
    ck.add_argument("--json", action="store_true")
    ck.set_defaults(func=run_check)

    mg = sub.add_parser("merge", help="Write one store holding every robot's records. Sources are untouched.")
    mg.add_argument("stores", nargs="+")
    mg.add_argument("--dest", required=True, help="An empty store to write.")
    mg.add_argument("--json", action="store_true")
    mg.set_defaults(func=run_merge)


def _guard(fn):
    def wrapped(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except ValueError as exc:
            print(f"[commontrace] error: {exc}", file=sys.stderr)
            return 2
    return wrapped


@_guard
def run_adopt(args: argparse.Namespace) -> int:
    done = fleet.adopt(args.source, args.dest)
    print(f"[commontrace] {args.dest} now randomizes like {args.source}: salt {done['salt']!r}, "
          f"holdout {done['rate']:.0%}" + (f", environment {done['env']}" if done["env"] else "")
          + (f", protecting {', '.join(done['protected_prefixes'])}" if done["protected_prefixes"] else ""))
    return 0


def _print_report(report: fleet.CheckReport) -> None:
    for s in report.stores:
        print(f"  {s.path}: {s.assignments:,} assignments, {s.outcomes:,} outcomes"
              + (f", {s.corrupt} unreadable line(s)" if s.corrupt else "")
              + f"  [salt {s.salt!r}, {s.rate:.0%}" + (f", {s.env}" if s.env else "") + "]")
    for problem in report.problems:
        print(f"  CANNOT POOL: {problem}")
    for conflict in report.conflicts[:10]:
        print(f"  CONFLICT: {conflict}")
    if len(report.conflicts) > 10:
        print(f"  ... and {len(report.conflicts) - 10} more conflicts")
    if report.duplicates:
        print(f"  {report.duplicates} identical record(s) appear in more than one store and collapse to one")


@_guard
def run_check(args: argparse.Namespace) -> int:
    report = fleet.check(args.stores)
    if args.json:
        import dataclasses
        print(json.dumps({**dataclasses.asdict(report), "mergeable": report.mergeable}, indent=2))
    else:
        _print_report(report)
        print("mergeable" if report.mergeable else "NOT mergeable")
    return 0 if report.mergeable else 1


@_guard
def run_merge(args: argparse.Namespace) -> int:
    report = fleet.check(args.stores)
    if not report.mergeable:
        _print_report(report)
        print("NOT merged", file=sys.stderr)
        return 1
    totals = fleet.merge(args.stores, args.dest, report=report)
    if args.json:
        print(json.dumps(totals, indent=2))
    else:
        print(f"[commontrace] merged {len(args.stores)} stores into {args.dest}: "
              f"{totals['assignments']:,} assignments, {totals['outcomes']:,} outcomes"
              + (f" ({totals['duplicates']} duplicate record(s) collapsed)" if totals["duplicates"] else ""))
        print(f"  commontrace experiment --dest {args.dest}   # or: commontrace proof status --dest {args.dest}")
    return 0
