"""`commontrace observation`: list, show, and consolidate reinforced facts."""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import observations, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "observation",
        help="Evidence-grounded observations consolidated from reinforced facts.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_list = sub.add_parser("list", help="List stored observations.")
    p_list.add_argument("--trend", default="", choices=("", *observations.TRENDS))
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--dest", default=None)
    p_list.set_defaults(func=run_list)

    p_show = sub.add_parser("show", help="Show one observation with its evidence.")
    p_show.add_argument("id", help="Observation id (obs-...).")
    p_show.add_argument("--json", action="store_true")
    p_show.add_argument("--dest", default=None)
    p_show.set_defaults(func=run_show)

    p_con = sub.add_parser("consolidate", help="Fold reinforced facts (confirmations>=2) into observations.")
    p_con.add_argument("--json", action="store_true")
    p_con.add_argument("--dest", default=None)
    p_con.set_defaults(func=run_consolidate)


def run_list(args: argparse.Namespace) -> int:
    rows = observations.load_observations(paths.resolve_root(args.dest))
    ordered = [rows[key] for key in sorted(rows)]
    if args.trend:
        ordered = [o for o in ordered if o.trend == args.trend]
    if args.json:
        print(json.dumps([o.to_dict() for o in ordered], indent=2))
        return 0
    if not ordered:
        print("No observations stored. Run `commontrace observation consolidate`.")
        return 0
    print(f"{'ID':<18} {'TREND':<14} {'PROOFS':>6}  STATEMENT")
    print("-" * 80)
    for o in ordered:
        statement = o.statement if len(o.statement) <= 45 else o.statement[:42] + "..."
        print(f"{o.id:<18} {o.trend:<14} {o.proof_count:>6}  {statement}")
    return 0


def run_show(args: argparse.Namespace) -> int:
    observation = observations.get_observation(paths.resolve_root(args.dest), args.id)
    if observation is None:
        print(f"[commontrace] no observation {args.id!r}; try `observation list`.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(observation.to_dict(), indent=2))
        return 0
    print(f"# {observation.id}  (trend: {observation.trend}, proofs: {observation.proof_count},"
          f" boost: {observations.observation_boost(observation.proof_count)})")
    print(observation.statement)
    print()
    print("## Evidence")
    for entry in observation.evidence:
        print(f"- [{entry.get('source_id') or '-'}] {entry.get('quote', '')}"
              f"{('  (' + str(entry['at']) + ')') if entry.get('at') else ''}")
    return 0


def run_consolidate(args: argparse.Namespace) -> int:
    try:
        results = observations.consolidate_facts(paths.resolve_root(args.dest))
    except Exception as exc:  # noqa: BLE001
        print(f"[commontrace] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([o.to_dict() for o in results], indent=2))
        return 0
    print(f"Consolidated {len(results)} observation(s) from reinforced facts (confirmations>=2).")
    for o in results:
        print(f"  {o.id}  [{o.trend}]  proofs={o.proof_count}  {o.statement[:60]}")
    return 0
