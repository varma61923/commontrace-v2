"""`commontrace function`: the function kits (see commontrace/functions.py)."""
from __future__ import annotations

import argparse
import sys

from commontrace import functions, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "function",
        help="Function kits: how CommonTrace measures support, sales, HR, coding, marketing, "
             "robotics, legal, finance, clinical -- or any function you define.",
    )
    sub = p.add_subparsers(dest="function_cmd", required=True)

    ls = sub.add_parser("list", help="The built-in kits.")
    ls.set_defaults(func=run_list)

    show = sub.add_parser("show", help="One kit's occasion, outcome model and planning assumptions.")
    show.add_argument("name", nargs="?", help="A kit key or a kit file. Default: this store's kit.")
    show.add_argument("--dest", default=None)
    show.set_defaults(func=run_show)

    fc = sub.add_parser("forecast", help="At your volume, how long until a verdict.")
    fc.add_argument("name", nargs="?", help="A kit key or a kit file. Default: this store's kit.")
    fc.add_argument("--daily", type=float, required=True, help="Occasions per day.")
    fc.add_argument("--baseline", type=float, default=None, help="Your current success rate.")
    fc.add_argument("--effect", type=float, default=None, help="Smallest effect worth detecting.")
    fc.add_argument("--rate", type=float, default=None, help="Holdout rate.")
    fc.add_argument("--within", type=int, default=None, metavar="DAYS",
                    help="Also say what daily volume a verdict within DAYS needs.")
    fc.add_argument("--dest", default=None)
    fc.set_defaults(func=run_forecast)

    demo = sub.add_parser(
        "demo", help="Fill an EMPTY store with synthetic, labelled holdout data so a report "
                     "can be shown before there is real data.")
    demo.add_argument("name", nargs="?", help="A kit key or a kit file. Default: this store's kit.")
    demo.add_argument("--seed", type=int, default=0)
    demo.add_argument("--dest", default=None)
    demo.set_defaults(func=run_demo)

    pr = sub.add_parser(
        "precision", help="Score the outcome detector against occasions a person has labelled.")
    pr.add_argument("sample", help="JSON lines: {occasion_id, truth, combine, signals}; see commontrace/precision.py.")
    pr.add_argument("--min-precision", type=float, default=0.9)
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=run_precision)

    chk = sub.add_parser("check", help="Validate a kit file.")
    chk.add_argument("file")
    chk.set_defaults(func=run_check)


def _kit(args: argparse.Namespace):
    if args.name:
        return functions.resolve(args.name)
    kit = functions.store_kit(paths.resolve_root(args.dest))
    if kit is None:
        raise functions.KitError(
            "this store has no function kit; name one (`commontrace function list`) or "
            "initialise with `commontrace init --function <name>`")
    return kit


def _guard(fn):
    def wrapped(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except ValueError as exc:  # KitError, bad forecast inputs
            print(f"[commontrace] error: {exc}", file=sys.stderr)
            return 2
    return wrapped


@_guard
def run_list(args: argparse.Namespace) -> int:
    for key, kit in sorted(functions.builtin_kits().items()):
        flag = "  (regulated)" if kit.regulated else ""
        print(f"{key:10s} {kit.title:26s} occasion: {kit.occasion_label:26s} "
              f"wait {kit.outcome.window_days:g}d{flag}")
    return 0


@_guard
def run_show(args: argparse.Namespace) -> int:
    print(functions.render_kit(_kit(args)))
    return 0


@_guard
def run_forecast(args: argparse.Namespace) -> int:
    print(functions.render_forecast(functions.forecast(
        _kit(args), args.daily, baseline=args.baseline, effect=args.effect,
        rate=args.rate, within_days=args.within,
    )))
    return 0


@_guard
def run_demo(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    kit = _kit(args)
    paths.warn_if_implicit_cwd_store(args.dest)
    functions.seed_demo(root, kit, seed=args.seed)
    print(f"[commontrace] wrote {functions.DEMO_OCCASIONS} synthetic {kit.key} occasions to {root}")
    print(f"  Every occasion id starts DEMO-{kit.key}- and the experiment note says "
          f"'{functions.DEMO_NOTE}'. Three memories: one helps, one hurts, one does nothing.")
    print(f"  commontrace experiment --dest {root}")
    return 0


@_guard
def run_check(args: argparse.Namespace) -> int:
    kit = functions.load_file(args.file)
    print(f"ok: {kit.key} ({kit.title}), agent_type {kit.agent_type}, "
          f"occasion {kit.occasion_label}, wait {kit.outcome.window_days:g}d, "
          f"detectors {', '.join(kit.outcome.signals)} ({kit.outcome.combine})")
    return 0



def run_precision(args: argparse.Namespace) -> int:
    import json

    from commontrace import precision

    try:
        with open(args.sample, encoding="utf-8") as fh:
            result = precision.evaluate(precision.read_sample(fh.read()))
    except (OSError, precision.SampleError) as exc:
        print(f"[commontrace] error: {exc}", file=sys.stderr)
        return 2
    met = result.precision is not None and result.precision >= args.min_precision
    if args.json:
        print(json.dumps({**result.to_dict(), "min_precision": args.min_precision, "met": met}, indent=2))
    else:
        print(precision.render(result, min_precision=args.min_precision))
        print("  " + ("meets the target." if met else "DOES NOT meet the target (or made no success calls)."))
    return 0 if met else 1
