"""Graded and multi-objective outcomes: configure a utility, record rewards, audit them, read effects."""

from __future__ import annotations

import argparse
import json
import sys

from unimem import holdout_io, paths, rewards


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "reward",
        help="Graded outcomes (task progress, evaluator scores, cost, energy) realized into the causal evidence.",
    )
    sub = parser.add_subparsers(dest="reward_cmd", required=True)
    conf = sub.add_parser("configure", help="Set the utility that scores named metrics into a reward in [0, 1].")
    conf.add_argument(
        "spec",
        help='JSON or @file: {"terms": {"progress": {"weight": 3}, "energy_wh": {"low": 0, "high": 50, '
        '"better": "lower"}}}',
    )
    rec = sub.add_parser("record", help="Record one occasion's graded outcome.")
    rec.add_argument("--occasion-id", required=True)
    group = rec.add_mutually_exclusive_group(required=True)
    group.add_argument("--reward", type=float, help="A reward in [0, 1].")
    group.add_argument("--metrics", help="JSON or @file object of named numbers, scored by the utility.")
    sub.add_parser("verify", help="Recompute every rounding draw and check it against the outcomes log.")
    eff = sub.add_parser("effects", help="Per-memory effects on the raw reward scale.")
    eff.add_argument("--json", action="store_true")
    for child in (conf, rec, eff, sub.choices["verify"]):
        child.add_argument("--dest", default=None)
        child.set_defaults(func=run)


def _json(value: str) -> object:
    if value.startswith("@"):
        with open(value[1:], encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(value)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        if args.reward_cmd == "configure":
            spec = _json(args.spec)
            if not isinstance(spec, dict):
                raise ValueError("the utility must be a JSON object")
            out = rewards.configure(root, spec)
            print(f"[unimem] utility {out['digest'][:23]} with {len(out['terms'])} term(s)")
            return 0
        if args.reward_cmd == "record":
            metrics = _json(args.metrics) if args.metrics else None
            if metrics is not None and not isinstance(metrics, dict):
                raise ValueError("metrics must be a JSON object")
            out = rewards.record(root, args.occasion_id, reward=args.reward, metrics=metrics)
            state = "recorded" if out["recorded"] else "already on record"
            print(
                f"[unimem] occasion {args.occasion_id!r}: reward {out['reward']:.4f} "
                f"realized as {'succeeded' if out['succeeded'] else 'failed'} ({state})"
            )
            return 0
        if args.reward_cmd == "verify":
            problems = rewards.verify(root)
            print("[unimem] rewards verify: " + ("OK" if not problems else f"{len(problems)} problem(s)"))
            for problem in problems:
                print("  " + problem)
            return 1 if problems else 0
        rows = rewards.effects(root)
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
            return 0
        if not rows:
            print("[unimem] no memory has both arms on occasions with a recorded reward yet")
        for r in rows:
            print(
                f"  {r['lesson']}: {r['effect']:+.3f} reward [{r['ci_low']:+.3f}, {r['ci_high']:+.3f}] "
                f"({r['mean_injected']:.3f} shown vs {r['mean_withheld']:.3f} withheld; "
                f"{r['n_injected']} / {r['n_withheld']})"
            )
        return 0
    except (OSError, ValueError, holdout_io.ConflictingOutcome) as exc:
        print(f"[unimem] reward: {exc}", file=sys.stderr)
        return 1
