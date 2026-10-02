"""`commontrace proof`: the Agent Learning Proof (see commontrace/proof.py)."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from commontrace import functions, paths, proof


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "proof",
        help="The Agent Learning Proof: plan, run, report and independently verify a randomized "
             "holdout on your own memory.",
    )
    sub = p.add_subparsers(dest="proof_cmd", required=True)

    st = sub.add_parser("start", help="Forecast, start the holdout, and register the design before any data.")
    st.add_argument("function", nargs="?", help="A kit key or kit file (default: this store's kit).")
    st.add_argument("--label", required=True, help="Whose proof this is, e.g. the customer or fleet.")
    st.add_argument("--daily", type=float, required=True, help="Occasions per day.")
    st.add_argument("--value-per-occasion", type=float, default=None,
                    help="What one improved occasion is worth to you; adds a value ledger.")
    st.add_argument("--baseline", type=float, default=None, help="Your current success rate.")
    st.add_argument("--effect", type=float, default=None, help="Smallest effect worth detecting.")
    st.add_argument("--rate", type=float, default=None, help="Holdout rate.")
    st.add_argument("--max-days", type=int, default=proof.MAX_DAYS)
    st.add_argument("--force", action="store_true",
                    help="Start even if it cannot answer in time, or replace a proof in progress.")
    st.add_argument("--dest", default=None)
    st.set_defaults(func=run_start)

    ss = sub.add_parser("status", help="How far along, whether it can be trusted, what each memory shows.")
    ss.add_argument("--json", action="store_true")
    ss.add_argument("--dest", default=None)
    ss.set_defaults(func=run_status)

    rp = sub.add_parser("report", help="Write the package: report, machine-readable record, raw rows.")
    rp.add_argument("--out", default=None, help="Directory to write (default: proof-<label>/).")
    rp.add_argument("--key-file", default=None, help="Sign with this key (>= 16 bytes of secret).")
    rp.add_argument("--org", default="", help="Name bound into the signature (default: the label).")
    rp.add_argument("--dest", default=None)
    rp.set_defaults(func=run_report)

    vf = sub.add_parser("verify", help="Recompute a package from its raw data and check every commitment.")
    vf.add_argument("directory")
    vf.add_argument("--key-file", default=None, help="The issuer's key, to check the signature.")
    vf.set_defaults(func=run_verify)

    wz = sub.add_parser(
        "wizard", help="Plan, confirm, start and (optionally) rehearse a proof in one guided flow.")
    wz.add_argument("function", nargs="?", help="A kit key or kit file (default: this store's kit).")
    wz.add_argument("--label", default=None, help="Whose proof this is.")
    wz.add_argument("--daily", type=float, default=None, help="Occasions per day.")
    wz.add_argument("--value-per-occasion", type=float, default=None)
    wz.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")
    wz.add_argument("--simulate", action="store_true",
                    help="Rehearse on a simulated fleet in a FRESH store: start, run planted memories "
                         "through the real path, report, verify, and say whether the verdicts match.")
    wz.add_argument("--seed", type=int, default=0)
    wz.add_argument("--dest", default=None)
    wz.set_defaults(func=run_wizard)

    dm = sub.add_parser("demo", help="A proof on synthetic data, to show a report before there is real data.")
    dm.add_argument("function", nargs="?", help="A kit key or kit file (default: this store's kit).")
    dm.add_argument("--value-per-occasion", type=float, default=None)
    dm.add_argument("--seed", type=int, default=0)
    dm.add_argument("--dest", default=None)
    dm.set_defaults(func=run_demo)


def _guard(fn):
    def wrapped(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except ValueError as exc:
            print(f"[commontrace] error: {exc}", file=sys.stderr)
            return 2
    return wrapped


def _kit(args: argparse.Namespace, root: str):
    if args.function:
        return functions.resolve(args.function)
    kit = functions.store_kit(root)
    if kit is None:
        raise proof.ProofError("name a function (`commontrace function list`) or "
                               "`commontrace init --function <name>` first")
    return kit


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "proof"


@_guard
def run_start(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    paths.warn_if_implicit_cwd_store(args.dest)
    kit = _kit(args, root)
    state, fc = proof.start(
        root, kit, label=args.label, daily=args.daily, value_per_occasion=args.value_per_occasion,
        baseline=args.baseline, effect=args.effect, rate=args.rate, max_days=args.max_days,
        force=args.force)
    print(functions.render_forecast(fc))
    reg = state["preregistration"]
    print(f"\n[commontrace] proof started for {state['label']!r}. Registered before any data:")
    print(f"  outcome:  {reg['primary_outcome']}")
    print(f"  smallest effect that matters {reg['minimum_practical_effect']:.0%}, holdout "
          f"{reg['holdout_rate']:.0%}, {reg['planned_occasions']:,} occasions, stopping rule {reg['stopping_rule']}")
    print("  Retrieve with an occasion id and report each outcome under the same id; then:")
    print("    commontrace proof status     # progress, validity, verdicts so far")
    print("    commontrace proof report     # the signed, verifiable package")
    return 0


@_guard
def run_status(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    s = proof.status(root)
    if args.json:
        import dataclasses
        print(json.dumps(dataclasses.asdict(s), indent=2))
    else:
        print(proof.render_status(s))
    return 0 if s.state != "compromised" else 1


@_guard
def run_report(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    state = proof.load_state(root)
    if state is None:
        raise proof.ProofError("no proof has been started here (`commontrace proof start`)")
    key = proof.read_key(args.key_file) if args.key_file else None
    out = args.out or f"proof-{_slug(state['label'])}"
    record = proof.build(root, out, key=key, org_id=args.org)
    print(f"[commontrace] wrote {os.path.join(out, proof.PAGE_NAME)} (open or send this), "
          f"{proof.MARKDOWN_NAME}, {proof.RECORD_NAME} and {proof.DATA_NAME}")
    print(f"  {'FINAL' if record['final'] else 'INTERIM'}; integrity {record['integrity']['verdict']}; "
          f"{'signed' if record['signature'] else 'NOT signed (--key-file)'}"
          + ("; SYNTHETIC DEMO DATA" if record["synthetic"] else ""))
    print(f"  check it:  commontrace proof verify {out}" + (" --key-file KEY" if record["signature"] else ""))
    return 0


@_guard
def run_verify(args: argparse.Namespace) -> int:
    key = proof.read_key(args.key_file) if args.key_file else None
    checks = proof.verify(args.directory, key=key)
    print(proof.render_checks(checks))
    ok = proof.verified(checks)
    print("\n" + ("verified: every check that could run passed." if ok else
                  "NOT VERIFIED: the package does not match its own data."))
    return 0 if ok else 1


@_guard
def run_demo(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    paths.warn_if_implicit_cwd_store(args.dest)
    kit = _kit(args, root)
    state = proof.start_demo(root, kit, value_per_occasion=args.value_per_occasion, seed=args.seed)
    print(f"[commontrace] synthetic {kit.key} proof written to {root} (label {state['label']!r}).")
    print("  commontrace proof status\n  commontrace proof report --key-file KEY")
    return 0


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or (default or "")


@_guard
def run_wizard(args: argparse.Namespace) -> int:
    """Plan, confirm, start; with --simulate, rehearse the whole path and check the verdicts."""
    root = paths.resolve_root(args.dest)
    paths.warn_if_implicit_cwd_store(args.dest)
    interactive = sys.stdin.isatty() and not args.yes
    function = args.function
    if function is None and functions.store_kit(root) is None:
        if not interactive:
            raise proof.ProofError("name a function (`commontrace function list`)")
        function = _ask("Function (" + ", ".join(sorted(functions.builtin_kits())) + ")")
    args.function = function
    kit = _kit(args, root)
    label = args.label or (_ask("Whose proof is this (a customer or fleet name)") if interactive else "")
    daily = args.daily
    if daily is None and interactive:
        daily = float(_ask(f"How many {kit.occasion_label}s per day"))
    if not label or daily is None:
        raise proof.ProofError("the wizard needs --label and --daily when it cannot ask")
    vpo = args.value_per_occasion
    if vpo is None and interactive:
        raw = _ask("What is one improved occasion worth to you (blank to skip)")
        vpo = float(raw) if raw else None

    fc = proof._design(kit, daily, None, None, None)
    print(functions.render_forecast(fc))
    if interactive and _ask("Start this proof? (y/n)", "y").lower() not in ("y", "yes"):
        print("[commontrace] nothing was started.")
        return 0
    state, _ = proof.start(root, kit, label=label, daily=daily, value_per_occasion=vpo,
                           force=args.simulate)
    reg = state["preregistration"]
    print(f"\n[commontrace] proof started for {state['label']!r}; registered before any data: "
          f"{reg['planned_occasions']:,} occasions, stopping rule {reg['stopping_rule']}.")
    if not args.simulate:
        print("  Point your agents at the gateway and report each outcome under the same occasion id:")
        print("    commontrace gateway          # any language, any robot (HTTP + JSON or --stdio)")
        print("    commontrace proof status     # progress and verdicts as they form")
        print("    commontrace proof report     # the page and package to share")
        return 0

    sim = proof.simulate_fleet(root, kit, seed=args.seed)
    out = f"proof-{_slug(state['label'])}"
    record = proof.build(root, out)
    checks = proof.verify(out)
    found = {m["lesson_slug"]: m["verdict"] for m in record["memories"]}
    wrong = {slug: (f"{effect:+.0%}", found.get(slug))
             for slug, effect in sim["planted"].items() if not proof.verdict_matches(effect, found.get(slug))}
    print(f"\n[commontrace] rehearsal: {sim['occasions']:,} simulated occasions through the real path.")
    for slug, effect in sim["planted"].items():
        print(f"  {slug:22s} planted {effect:+.0%}  ->  {found.get(slug)}")
    print(f"  integrity {record['integrity']['verdict']}; package verifies: {proof.verified(checks)}")
    print(f"  page: {os.path.join(out, proof.PAGE_NAME)}")
    ok = not wrong and proof.verified(checks) and record["integrity"]["verdict"] == "SOUND"
    print("  " + ("every verdict matches what was planted." if ok else f"MISMATCH: {wrong}"))
    return 0 if ok else 1
