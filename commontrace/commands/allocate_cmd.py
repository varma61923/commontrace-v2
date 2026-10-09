"""`commontrace allocate`: adaptive holdout allocation in published, hash-chained eras."""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import allocation, holdout_io, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "allocate",
        help="Adaptive holdout: explore uncertain memories at a high rate, monitor proven ones at a low one.",
    )
    sub = p.add_subparsers(dest="allocate_cmd", required=True)

    en = sub.add_parser("enable", help="Start adaptive allocation for the running experiment.")
    en.add_argument("--explore", type=float, default=allocation.EXPLORE_RATE,
                    help="Holdout rate while a memory is unproven (default 0.5).")
    en.add_argument("--monitor", type=float, default=allocation.MONITOR_RATE,
                    help="Holdout rate once a memory is proven (default 0.05).")
    en.add_argument("--era", type=int, default=allocation.ERA_OCCASIONS,
                    help="Occasions per era before the next plan (default 250).")
    en.set_defaults(func=run_enable)

    pl = sub.add_parser("plan", help="Publish the next era's rates from every outcome so far.")
    pl.add_argument("--if-due", action="store_true", help="Only when the current era has run its occasions.")
    pl.set_defaults(func=run_plan)

    sh = sub.add_parser("show", help="The schedule in force and its rates.")
    sh.add_argument("--json", action="store_true")
    sh.set_defaults(func=run_show)

    ve = sub.add_parser("verify", help="Check the schedule chain and every logged rate against it.")
    ve.set_defaults(func=run_verify)

    di = sub.add_parser("disable", help="Return to the experiment's fixed rate from the next assignment on.")
    di.set_defaults(func=run_disable)

    for parser in (en, pl, sh, ve, di):
        parser.add_argument("--dest", default=None)


def _guarded(fn):
    def run(args: argparse.Namespace) -> int:
        try:
            return fn(args, paths.resolve_root(args.dest))
        except (OSError, ValueError) as exc:
            print(f"[commontrace] allocate: {exc}", file=sys.stderr)
            return 1
    return run


def _describe(s: allocation.Schedule) -> str:
    lines = [f"[commontrace] schedule v{s.version} ({s.digest[:16]}), in force from {s.effective_from}",
             f"  unlisted memories: {s.default_rate:.0%} withheld"]
    lines += [f"  {lesson}: {rate:.0%} withheld" for lesson, rate in s.rates.items()]
    return "\n".join(lines)


@_guarded
def run_enable(args, root) -> int:
    schedule = allocation.enable(root, allocation.Policy(args.explore, args.monitor, args.era))
    print(_describe(schedule))
    return 0


@_guarded
def run_plan(args, root) -> int:
    config = holdout_io.load_config(root)
    if args.if_due and not allocation.due(root, config.salt):
        print("[commontrace] the current era has not run its occasions yet; nothing published")
        return 0
    schedule = allocation.plan(root)
    print(_describe(schedule))
    return 0


@_guarded
def run_show(args, root) -> int:
    config = holdout_io.load_config(root)
    schedule = allocation.active(root, config.salt)
    if args.json:
        print(json.dumps(None if schedule is None else {**schedule.body(), "digest": schedule.digest},
                         indent=2, sort_keys=True))
        return 0
    if schedule is None:
        print(f"[commontrace] adaptive allocation is off; every memory is withheld at {config.rate:.0%}")
        return 0
    print(_describe(schedule))
    return 0


@_guarded
def run_verify(args, root) -> int:
    from commontrace.commands import experiment_cmd

    problems = allocation.verify(root)
    rows, _rate, _corrupt = experiment_cmd._load(root)
    wrong = allocation.unscheduled([r for r in rows if r.schedule], allocation.history(root))
    problems += [f"{r.lesson} on {r.occasion_id} was assigned at {r.rate:.0%}, not its schedule's rate"
                 for r in wrong[:20]]
    if problems:
        print("[commontrace] allocation NOT verified:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return 1
    print(f"[commontrace] {len(allocation.history(root))} schedule(s) chain correctly; "
          f"every scheduled assignment used its schedule's rate")
    return 0


@_guarded
def run_disable(args, root) -> int:
    schedule = allocation.disable(root)
    print(f"[commontrace] adaptive allocation off from {schedule.effective_from}")
    return 0
