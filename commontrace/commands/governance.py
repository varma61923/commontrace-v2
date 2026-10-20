"""Related CLI operations with lazy, operation-local dependencies."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from commontrace import allocation, fleet


# policy operations
def policy_add_parser(subparsers):
    parser = subparsers.add_parser("policy", help="Held-out conditional policy evaluation and release gate.")
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("evaluate", "train", "install", "register"):
        child = sub.add_parser(name)
        child.add_argument("--dest")
        child.add_argument("--minimum-samples", type=int, default=30)
        if name == "register":
            child.add_argument("--evaluation-samples", type=int, default=300)
        if name != "train":
            child.add_argument("candidate", help="JSON delivery policy, unchanged retrieval eligibility/baseline.")
        child.set_defaults(func=policy_run)


def policy_run(args):
    import json
    import sys

    from commontrace import paths, policy

    root = paths.resolve_root(args.dest)
    try:
        if args.operation == "train":
            result = policy.train(root, minimum_cases=args.minimum_samples)
        else:
            with open(args.candidate, encoding="utf-8") as fh:
                candidate = json.load(fh)
            operation = {"install": policy.install, "evaluate": policy.evaluate, "register": policy.preregister}[
                args.operation
            ]
            if args.operation == "register":
                result = operation(root, candidate, evaluation_samples=args.evaluation_samples)
            else:
                result = operation(root, candidate, minimum_samples=args.minimum_samples)
        print(json.dumps(result, indent=2))
        return 0 if args.operation in ("train", "register") or result["safe_to_release"] else 1
    except (ValueError, OSError, PermissionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


# assurance operations
def assurance_add_parser(subparsers):
    parser = subparsers.add_parser("assurance", help="Sign, audit, replay and measure memory operations.")
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("signing", "provenance", "forget-certificate", "forensics", "action-vote", "cost"):
        child = sub.add_parser(name)
        child.add_argument("--dest")
        if name == "signing":
            child.add_argument("--algorithm", choices=("ed25519", "hmac-sha256"), default="ed25519")
        elif name != "cost":
            child.add_argument("id")
        if name in ("forensics", "action-vote"):
            child.add_argument("--evaluator", required=True, help="Trusted local module:function callback.")
            child.add_argument("--seed", type=int, default=0)
            child.add_argument("--seconds", type=float, default=30)
        if name == "action-vote":
            child.add_argument("--action", required=True, help="JSON action file.")
        child.set_defaults(func=assurance_run)


def assurance_run(args):
    import importlib
    import json
    import sys

    from commontrace import assurance, memory_authority, paths

    root = paths.resolve_root(args.dest)
    try:
        if args.operation == "signing":
            result = memory_authority.configure_signing(root, algorithm=args.algorithm)
        elif args.operation == "provenance":
            result = memory_authority.provenance(root, args.id)
        elif args.operation == "forget-certificate":
            result = memory_authority.forgetting_certificate(root, args.id)
        elif args.operation == "cost":
            result = assurance.cost_report(root)
        else:
            module, name = args.evaluator.split(":", 1)
            evaluator = getattr(importlib.import_module(module), name)
            if args.operation == "forensics":
                result = assurance.forensics(root, args.id, evaluator, seed=args.seed, seconds=args.seconds)
            else:
                with open(args.action, encoding="utf-8") as fh:
                    action = json.load(fh)
                result = assurance.action_vote(root, args.id, action, evaluator, seed=args.seed, seconds=args.seconds)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1 if result.get("allowed") is False else 0
    except (ValueError, OSError, PermissionError, ImportError, AttributeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


# gate operations
def gate_add_parser(subparsers: argparse._SubParsersAction) -> None:
    from commontrace import gate

    p = subparsers.add_parser(
        "gate",
        help="Exit non-zero if this store's memory should not ship: a compromised experiment, "
        "an active lesson measured to hurt, unsafe or unfinished lesson text.",
        description=(
            "Run in CI before a release is promoted. Exit 0 means every blocking check passed; "
            "exit 1 means at least one failed (each is named). Warnings (contradicting lessons, "
            "no verdicts yet) block only with --strict."
        ),
    )
    p.add_argument("--dest", default=None, help="Store root (default: auto-detected).")
    p.add_argument("--strict", action="store_true", help="Treat warnings as failures.")
    p.add_argument(
        "--format",
        choices=sorted(gate.RENDERERS),
        default="text",
        help="text (default), json, junit (for CI test reports), or github (Actions annotations).",
    )
    p.add_argument("--output", default=None, help="Write the report to this file instead of stdout.")
    p.add_argument(
        "--policy", help="JSON candidate exploration delivery policy; block unidentified or harmful changes."
    )
    p.set_defaults(func=gate_run)


def gate_run(args: argparse.Namespace) -> int:
    from commontrace import gate, paths

    root = paths.resolve_root(args.dest)
    result = gate.run(root, strict=args.strict)
    if getattr(args, "policy", None):
        import json

        from commontrace import policy

        try:
            with open(args.policy, encoding="utf-8") as fh:
                report = policy.evaluate(root, json.load(fh))
            result.checks.append(
                gate.Check(
                    "policy",
                    gate.PASS if report["safe_to_release"] else gate.FAIL,
                    report.get("reason", json.dumps(report)),
                )
            )
        except (ValueError, OSError, PermissionError) as exc:
            result.checks.append(gate.Check("policy", gate.FAIL, str(exc)))
    rendered = gate.RENDERERS[args.format](result)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        print(gate.render_text(result) if args.format != "text" else rendered)
    else:
        print(rendered)
    return 0 if result.passed else 1


# conformance operations
def conformance_add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "conformance", help="Check an implementation against the protocol: vectors, a store, or a gateway."
    )
    sub = p.add_subparsers(dest="conformance_cmd", required=True)
    v = sub.add_parser("vectors", help="Write or check the reference vectors (protocol/conformance/vectors.json).")
    v.add_argument("--write", action="store_true", help="Regenerate the file from the reference implementation.")
    v.set_defaults(func=conformance_run_vectors)
    e = sub.add_parser("exec", help="Run your program against the vectors over stdio (one JSON object per line).")
    e.add_argument("command", help='e.g. "./my-impl" or "python3 -m commontrace.conformance"')
    e.add_argument("--vectors", default=None)
    e.add_argument(
        "--only",
        default=None,
        help="Comma-separated ops to check (assign,ledger,digest,revision): "
        "an implementation may cover a subset, and the report says which.",
    )
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=conformance_run_exec)
    s = sub.add_parser("store", help="Check a local store's files and assignments.")
    s.add_argument("--dest", default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=conformance_run_store)
    g = sub.add_parser("gateway", help="Drive a running gateway's HTTP API and check its behaviour.")
    g.add_argument("url")
    g.add_argument("--token", required=True)
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=conformance_run_gateway)


def conformance__emit(results, as_json: bool) -> int:
    import json

    from commontrace import conformance

    print(json.dumps(conformance.to_dict(results), indent=2) if as_json else conformance.render(results))
    return 0 if all(r.ok for r in results) else 1


def conformance_run_vectors(args: argparse.Namespace) -> int:
    import json

    from commontrace import conformance

    if args.write:
        text = json.dumps(conformance.build_vectors(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"
        for path in (conformance.VECTORS_PATH, conformance.SPEC_VECTORS_PATH):
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            print(f"wrote {path}")
        return 0
    return conformance__emit(conformance.check_vectors_against_reference(conformance.load_vectors()), False)


def conformance_run_exec(args: argparse.Namespace) -> int:
    import sys

    from commontrace import conformance

    only = tuple(x.strip() for x in args.only.split(",")) if args.only else None
    if only and set(only) - set(conformance.OPS):
        print("[commontrace] --only takes " + ", ".join(conformance.OPS), file=sys.stderr)
        return 2
    return conformance__emit(
        conformance.run_exec(args.command, conformance.load_vectors(args.vectors), only=only), args.json
    )


def conformance_run_store(args: argparse.Namespace) -> int:
    from commontrace import conformance, paths

    return conformance__emit(conformance.check_store(paths.resolve_root(args.dest)), args.json)


def conformance_run_gateway(args: argparse.Namespace) -> int:
    import sys

    from commontrace import conformance

    results = conformance.check_gateway(args.url, args.token)
    if results and results[0].detail.startswith("HTTP 0"):
        print(f"[commontrace] cannot reach {args.url}", file=sys.stderr)
    return conformance__emit(results, args.json)


# defense operations
def defense_add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "defense",
        help="Screen content for secrets, credentials, PII, and injection patterns.",
        description="Inspect and redact sensitive data using 40+ regex patterns and checksums.",
    )
    sub = parser.add_subparsers(dest="defense_op", required=True)
    red = sub.add_parser("redteam", help="Run seeded synthetic poisoning exercises on an isolated shadow store.")
    red.add_argument("--seed", type=int, default=0)
    red.add_argument("--occasions", type=int, default=128)

    # screen
    p_screen = sub.add_parser("screen", help="Screen text or file for sensitive patterns.")
    p_screen.add_argument("text", nargs="?", help="Text to screen (or omit to read from stdin).")
    p_screen.add_argument("--file", help="File to read content from.")
    p_screen.add_argument("--action", default="redact", choices=["allow", "redact", "block"], help="Policy action.")
    p_screen.add_argument("--json", action="store_true", help="Output results as JSON.")

    parser.set_defaults(func=defense_run)


def defense_run(args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import defense

    op = args.defense_op
    if op == "redteam":
        from commontrace import redteam

        print(json.dumps(redteam.run(seed=args.seed, occasions=args.occasions), indent=2))
        return 0
    if op == "screen":
        content = ""
        if args.file:
            with open(args.file, encoding="utf-8") as f:
                content = f.read()
        elif args.text:
            content = args.text
        else:
            content = sys.stdin.read()

        policy = defense.DefensePolicy(
            enabled=True,
            rules=(defense.PolicyRule(on="sensitive_data", action=defense.DefenseAction(args.action)),),
        )
        decision = defense.screen_content(content, policy=policy)

        if args.json:
            print(
                json.dumps(
                    {
                        "action": decision.action.value,
                        "detector": decision.detector,
                        "message": decision.message,
                        "redacted_content": decision.redacted_content,
                        "matched_types": decision.matched_types,
                        "hits": decision.hits,
                    },
                    indent=2,
                )
            )
            return 0 if decision.action != defense.DefenseAction.BLOCK else 2

        if decision.matched_types:
            print(f"[{decision.action.value.upper()}] Matches found: {', '.join(decision.matched_types)}")
            for hit in decision.hits:
                print(f"  - {hit['detector']}: {hit['preview']}")
            if decision.action == defense.DefenseAction.REDACT and decision.redacted_content:
                print("\nRedacted Content:\n" + decision.redacted_content)
        else:
            print("[ALLOW] No sensitive patterns detected.")
        return 0 if decision.action != defense.DefenseAction.BLOCK else 2

    return 0


# compression operations
def compression_add_parser(subparsers):
    from commontrace import compression

    parser = subparsers.add_parser("compression", help="Trace→episode→observation→lesson→skill→directive ladder.")
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("propose", "register", "trial", "outcome", "evaluate", "review", "active", "export-training"):
        child = sub.add_parser(name)
        child.add_argument("--dest")
        if name not in ("propose", "active", "export-training"):
            child.add_argument("id")
        if name == "propose":
            child.add_argument("text")
            child.add_argument("--level", choices=compression.LEVELS[1:], required=True)
            child.add_argument("--source", action="append", required=True)
            child.add_argument("--parent", default="")
            child.add_argument("--actor", required=True)
            child.add_argument("--applies-when", required=True)
            child.add_argument("--do-not-apply-when", required=True)
            child.add_argument("--deny-tool", action="append", default=[])
        if name == "register":
            child.add_argument("--trials", type=int, default=300)
        if name == "outcome":
            child.add_argument("value", type=float)
        if name == "export-training":
            child.add_argument(
                "--format",
                choices=("sft", "dpo"),
                default="sft",
                help="sft: admitted texts; dpo: preference pairs from decisive randomized trials",
            )
        if name == "review":
            child.add_argument("--experiment", required=True)
            child.add_argument("--actor", required=True)
            child.add_argument("--expected-revision", required=True)
        child.set_defaults(func=compression_run)


def compression_run(args):
    import json
    import sys

    from commontrace import compression, paths

    root, op = paths.resolve_root(args.dest), args.operation
    try:
        if op == "propose":
            result = compression.propose(
                root,
                args.text,
                level=args.level,
                sources=args.source,
                parent=args.parent,
                actor=args.actor,
                applies_when=args.applies_when,
                do_not_apply_when=args.do_not_apply_when,
                deny_tools=args.deny_tool,
            )
        elif op == "register":
            result = compression.register(root, args.id, trials=args.trials)
        elif op == "trial":
            result = compression.next_trial(root, args.id)
        elif op == "outcome":
            result = compression.outcome(root, args.id, args.value)
        elif op == "evaluate":
            result = compression.evaluate(root, args.id)
        elif op == "review":
            result = compression.review(
                root, args.id, args.experiment, actor=args.actor, expected_revision=args.expected_revision
            )
        elif op == "active":
            result = compression.active(root)
        elif getattr(args, "format", "sft") == "dpo":
            result = compression.export_preferences(root)
        else:
            result = compression.export_training(root)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError, PermissionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


# allocate operations
def allocate_add_parser(subparsers: argparse._SubParsersAction) -> None:
    from commontrace import allocation

    p = subparsers.add_parser(
        "allocate",
        help="Adaptive holdout: explore uncertain memories at a high rate, monitor proven ones at a low one.",
    )
    sub = p.add_subparsers(dest="allocate_cmd", required=True)

    en = sub.add_parser("enable", help="Start adaptive allocation for the running experiment.")
    en.add_argument(
        "--explore",
        type=float,
        default=allocation.EXPLORE_RATE,
        help="Holdout rate while a memory is unproven (default 0.5).",
    )
    en.add_argument(
        "--monitor",
        type=float,
        default=allocation.MONITOR_RATE,
        help="Holdout rate once a memory is proven (default 0.05).",
    )
    en.add_argument(
        "--era",
        type=int,
        default=allocation.ERA_OCCASIONS,
        help="Occasions per era before the next plan (default 250).",
    )
    en.add_argument(
        "--mode",
        choices=allocation.MODES,
        default="verdict",
        help="verdict: explore/monitor rates from each memory's verdict (default). "
        "thompson: the causal gate injects each memory, per occasion context, with the "
        "posterior probability that it helps there.",
    )
    en.set_defaults(func=allocate_run_enable)

    pl = sub.add_parser("plan", help="Publish the next era's rates from every outcome so far.")
    pl.add_argument("--if-due", action="store_true", help="Only when the current era has run its occasions.")
    pl.add_argument("--require-certificate", action="store_true",
                    help="Refuse a Thompson schedule without a matching, unrevoked promotion certificate.")
    pl.set_defaults(func=allocate_run_plan)

    sh = sub.add_parser("show", help="The schedule in force and its rates.")
    sh.add_argument("--json", action="store_true")
    sh.set_defaults(func=allocate_run_show)

    ve = sub.add_parser("verify", help="Check the schedule chain and every logged rate against it.")
    ve.set_defaults(func=allocate_run_verify)

    di = sub.add_parser("disable", help="Return to the experiment's fixed rate from the next assignment on.")
    di.set_defaults(func=allocate_run_disable)

    ga = sub.add_parser(
        "gate",
        help="Causal gate: where each memory helps (per context), source reputations, effect drift, "
        "and an off-policy check of the gate on later occasions.",
    )
    ga.add_argument("--json", action="store_true")
    ga.add_argument("--floor", type=float, default=allocation.MONITOR_RATE,
                    help="Least withholding probability the gate may set (default 0.05).")
    ga.add_argument("--alpha", type=float, default=0.05, help="False-discovery rate for drift (default 0.05).")
    ga.add_argument("--min-arm", type=int, default=10, help="Least assignments per arm in a drift window.")
    ga.add_argument("--practical", type=float, default=0.10,
                    help="Smallest change in effect worth calling drift (default 0.10).")
    ga.set_defaults(func=allocate_run_gate)

    for parser in (en, pl, sh, ve, di, ga):
        parser.add_argument("--dest", default=None)


def allocate__guarded(fn):
    import sys

    from commontrace import paths

    def run(args: argparse.Namespace) -> int:
        try:
            return fn(args, paths.resolve_root(args.dest))
        except (OSError, ValueError) as exc:
            print(f"[commontrace] allocate: {exc}", file=sys.stderr)
            return 1

    return run


def allocate__describe(s: allocation.Schedule) -> str:

    lines = [
        f"[commontrace] schedule v{s.version} ({s.digest[:16]}), in force from {s.effective_from}",
        f"  unlisted memories: {s.default_rate:.0%} withheld",
    ]
    lines += [f"  {lesson}: {rate:.0%} withheld" for lesson, rate in s.rates.items()]
    for lesson, by_context in s.context_rates.items():
        lines += [f"    {lesson} in {context!r}: {rate:.0%} withheld" for context, rate in by_context.items()]
    return "\n".join(lines)


@allocate__guarded
def allocate_run_gate(args, root) -> int:
    import json

    from commontrace import causal_gate, conformal_gate

    out = causal_gate.report(root, alpha=args.alpha, min_arm=args.min_arm, practical=args.practical,
                             floor=args.floor)
    out["harm_budget"] = conformal_gate.report(root)
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    print(causal_gate.render(out))
    budget = out["harm_budget"]
    if budget.get("enabled"):
        print(f"\nHarm budget (alpha={budget.get('alpha')}, {budget.get('kind')}):")
        for stratum, row in sorted(budget.get("strata", {}).items()):
            threshold = row.get("threshold", 1.01)
            print(f"  {stratum or '(all contexts)'}: lambda = "
                  + ("abstain (no admissible threshold)" if threshold > 1 else f"{threshold:.2f}"))
        realized = budget.get("realized", {})
        rate = realized.get("ipw_injected_failure")
        print("  realized IPW injected-failure rate since gating: "
              + ("n/a" if rate is None else f"{rate:.4f}") + f" over {realized.get('n', 0)} occasion(s)")
    return 0


@allocate__guarded
def allocate_run_enable(args, root) -> int:
    from commontrace import allocation

    schedule = allocation.enable(root, allocation.Policy(args.explore, args.monitor, args.era, args.mode))
    print(allocate__describe(schedule))
    return 0


@allocate__guarded
def allocate_run_plan(args, root) -> int:
    from commontrace import allocation, holdout_io

    config = holdout_io.load_config(root)
    if args.if_due and not allocation.due(root, config.salt):
        print("[commontrace] the current era has not run its occasions yet; nothing published")
        return 0
    schedule = allocation.plan(root, require_certificate=args.require_certificate)
    print(allocate__describe(schedule))
    return 0


@allocate__guarded
def allocate_run_show(args, root) -> int:
    import json

    from commontrace import allocation, holdout_io

    config = holdout_io.load_config(root)
    schedule = allocation.active(root, config.salt)
    if args.json:
        print(
            json.dumps(
                None if schedule is None else {**schedule.body(), "digest": schedule.digest,
                                               "policy_hash": allocation.policy_hash(
                                                   allocation.Policy(**schedule.policy))},
                indent=2, sort_keys=True,
            )
        )
        return 0
    if schedule is None:
        print(f"[commontrace] adaptive allocation is off; every memory is withheld at {config.rate:.0%}")
        return 0
    print(allocate__describe(schedule))
    print(f"policy hash (certify with promote register artifact_hash): "
          f"{allocation.policy_hash(allocation.Policy(**schedule.policy))}")
    return 0


@allocate__guarded
def allocate_run_verify(args, root) -> int:
    import sys

    from commontrace import allocation
    from commontrace.commands import experiment_cmd

    problems = allocation.verify(root)
    rows, _rate, _corrupt = experiment_cmd._load(root)
    wrong = allocation.unscheduled([r for r in rows if r.schedule], allocation.history(root))
    problems += [
        f"{r.lesson} on {r.occasion_id} was assigned at {r.rate:.0%}, not its schedule's rate" for r in wrong[:20]
    ]
    if problems:
        print("[commontrace] allocation NOT verified:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return 1
    print(
        f"[commontrace] {len(allocation.history(root))} schedule(s) chain correctly; "
        f"every scheduled assignment used its schedule's rate"
    )
    return 0


@allocate__guarded
def allocate_run_disable(args, root) -> int:
    from commontrace import allocation

    schedule = allocation.disable(root)
    print(f"[commontrace] adaptive allocation off from {schedule.effective_from}")
    return 0


# bill operations
def bill_add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "bill", help="Invoice proven value from verified proof packages (no default prices; preview unless --commit)."
    )
    sub = p.add_subparsers(dest="bill_cmd", required=True)
    tp = sub.add_parser("template", help="Print the commercial terms a price schedule must set, all empty.")
    tp.set_defaults(func=bill_run_template)
    inv = sub.add_parser("invoice", help="Build one invoice from a schedule and proof packages.")
    inv.add_argument("--schedule", required=True, help="Your price schedule (see `bill template`).")
    inv.add_argument("--org", required=True)
    inv.add_argument("--period", required=True, help="A label billed once, e.g. 2026-Q1.")
    inv.add_argument(
        "--agent-months",
        type=float,
        required=True,
        help="Agents under management, summed over the months of the period.",
    )
    inv.add_argument(
        "--package", action="append", default=[], metavar="DIR", help="A proof package directory (repeatable)."
    )
    inv.add_argument("--key-file", default=None, help="The issuer's key, to check each package's signature.")
    inv.add_argument("--book", default=".", help="Directory holding billing.json (what was charged before).")
    inv.add_argument("--commit", action="store_true", help="Record the invoice in the book. Default: preview only.")
    inv.add_argument("--json", action="store_true")
    inv.set_defaults(func=bill_run_invoice)


def bill_run_template(_args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import pricing

    print(json.dumps({k: None for k in pricing._FIELDS}, indent=2))
    print(
        "\n# Every term is required; none has a default. value_basis is one of: "
        + ", ".join(pricing.VALUE_BASES)
        + ". cap_per_invoice may be null for no cap.",
        file=sys.stderr,
    )
    return 0


def bill_run_invoice(args: argparse.Namespace) -> int:
    import json
    import os
    import sys

    from commontrace import pricing, proof

    try:
        schedule = pricing.PriceSchedule.from_file(args.schedule)
        key = proof.read_key(args.key_file) if args.key_file else None
        book = pricing.read_book(args.book)
        inv = pricing.invoice(
            book,
            schedule,
            org=args.org,
            period=args.period,
            agent_months=args.agent_months,
            packages=args.package,
            key=key,
            commit=args.commit,
        )
        if args.commit:
            pricing.write_book(args.book, book)
            with open(os.path.join(args.book, f"invoice-{args.org}-{args.period}.json"), "w", encoding="utf-8") as fh:
                json.dump(inv.to_dict(), fh, indent=2)
    except (pricing.PricingError, proof.ProofError, ValueError) as exc:
        print(f"[commontrace] error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(inv.to_dict(), indent=2) if args.json else pricing.render(inv))
    if not args.commit:
        print("\n(preview: nothing was recorded; add --commit to record this invoice)", file=sys.stderr)
    return 0


# reliability operations
def reliability_add_parser(subparsers: argparse._SubParsersAction) -> None:
    from commontrace import reliability

    p = subparsers.add_parser(
        "reliability",
        help="Score lessons against outcomes (do they actually work?) and detect "
        "active lessons that contradict each other.",
    )
    p.add_argument("--min-evidence", type=int, default=reliability.DEFAULT_MIN_EVIDENCE)
    p.add_argument("--precision-floor", type=float, default=reliability.DEFAULT_PRECISION_FLOOR)
    p.add_argument(
        "--activation-overlap",
        type=float,
        default=reliability.DEFAULT_ACTIVATION_OVERLAP,
        help="How similar two activation conditions must be before a contradiction is possible.",
    )
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any lesson is HARMFUL or any high-severity contradiction exists. "
        "For CI on a curated corpus.",
    )
    p.add_argument("--dest", default=None)
    p.set_defaults(func=reliability_run)


def reliability_run(args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import evidence_io, paths, reliability

    root = paths.resolve_root(args.dest)
    lessons = evidence_io.load_active_lessons(root)
    evidence = evidence_io.load_evidence(root)

    if not evidence:
        uncaptured = evidence_io.uncaptured_retrieval_counts(root)
        if uncaptured:
            print(
                "[commontrace] no lesson can be scored yet: retrieval evidence exists "
                f"({sum(uncaptured.values())} occasion(s), holdout log), but no `capture` "
                "was ever recorded for any of them, so no outcome can be judged.\n"
                "  Retrieved with no captured outcome: "
                + ", ".join(f"`{slug}` ({n})" for slug, n in sorted(uncaptured.items())),
                file=sys.stderr,
            )
            return 0
        print(
            "[commontrace] no retrieval evidence found, so no lesson can be scored.\n"
            "  This report needs to know which lessons were injected into which decisions.\n"
            "  Sources it reads:\n"
            "    - memory/episodes/*.md  -> lessons_retrieved_by_alpha + lessons_hit + verdict\n"
            "    - memory/traces/*.md    -> extensions.lessons_retrieved + extensions.lessons_hit\n"
            "    - memory/holdout_log.jsonl -> retrieval alone, no outcome (a `--experiment`\n"
            "      call with no matching `capture`)\n"
            "  Until a retriever records what it injected, lesson quality can only be\n"
            "  judged by opinion.",
            file=sys.stderr,
        )
        return 0

    scores = reliability.score_lessons(evidence, min_evidence=args.min_evidence, precision_floor=args.precision_floor)
    contradictions = reliability.find_contradictions(
        lessons, reliability=scores, activation_overlap=args.activation_overlap
    )
    uncaptured = evidence_io.uncaptured_retrieval_counts(root)

    if args.json:
        import dataclasses

        print(
            json.dumps(
                {
                    "n_occasions": len(evidence),
                    "lessons": [dataclasses.asdict(s) for s in scores],
                    "contradictions": [dataclasses.asdict(c) for c in contradictions],
                    "uncaptured_retrievals": uncaptured,
                },
                indent=2,
            )
        )
    else:
        print(reliability.render(scores, contradictions, args.min_evidence, uncaptured))

    if args.strict:
        harmful = [s for s in scores if s.verdict == reliability.VERDICT_HARMFUL]
        high = [c for c in contradictions if c.severity == "high"]
        if harmful or high:
            print(
                f"\n[commontrace] --strict: {len(harmful)} harmful lesson(s), "
                f"{len(high)} high-severity contradiction(s).",
                file=sys.stderr,
            )
            return 1
    return 0


# signals operations
def signals_add_parser(subparsers: argparse._SubParsersAction) -> None:
    from commontrace import failure_signals

    p = subparsers.add_parser(
        "signals",
        help="Cluster this store's failing traces into named signals, and export one "
        "as a regression dataset for LangSmith/Braintrust.",
    )
    sub = p.add_subparsers(dest="signals_cmd", required=True)

    ls = sub.add_parser("list", help="List failure signals found in this store.")
    ls.add_argument("--agent-type", default=None)
    ls.add_argument(
        "--similarity-threshold",
        type=float,
        default=failure_signals.DEFAULT_SIMILARITY,
        help="Cosine similarity at which failures are one signal (average linkage, IDF-weighted).",
    )
    ls.add_argument("--min-cluster-size", type=int, default=2)
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--dest", default=None)
    ls.set_defaults(func=signals_run_list)

    exp = sub.add_parser(
        "export",
        help="Export one signal's occurrences as a regression dataset.",
    )
    exp.add_argument("name", help="A signal's exact name, from `signals list`.")
    exp.add_argument("--format", choices=("langsmith", "braintrust"), required=True)
    exp.add_argument("--agent-type", default=None)
    exp.add_argument("--similarity-threshold", type=float, default=failure_signals.DEFAULT_SIMILARITY)
    exp.add_argument("--min-cluster-size", type=int, default=2)
    exp.add_argument("--out", default=None, help="Output file. Default: stdout.")
    exp.add_argument("--dest", default=None)
    exp.set_defaults(func=signals_run_export)


def signals__build(args: argparse.Namespace, root: str):
    from commontrace import failure_signals

    return failure_signals.build_signals(
        root,
        agent_type=args.agent_type,
        similarity_threshold=args.similarity_threshold,
        min_cluster_size=args.min_cluster_size,
    )


def signals_run_list(args: argparse.Namespace) -> int:
    import json

    from commontrace import paths

    root = paths.resolve_root(args.dest)
    signals, _by_id = signals__build(args, root)
    if not signals:
        print(
            "[commontrace] no failure signals found (no traces recorded as a failure, "
            "or none repeat enough to cluster)."
        )
        return 0
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": s.name,
                        "size": s.size,
                        "trend": s.trend,
                        "affected_agents": s.affected_agents,
                        "first_seen": s.first_seen,
                        "last_seen": s.last_seen,
                        "trace_ids": s.trace_ids,
                    }
                    for s in signals
                ],
                indent=2,
            )
        )
        return 0
    for s in signals:
        agents = ", ".join(s.affected_agents) if s.affected_agents else "(not recorded)"
        print(f"  {s.name}")
        print(f"    size={s.size} trend={s.trend} agents={agents}")
        if s.first_seen or s.last_seen:
            print(f"    first_seen={s.first_seen or '?'} last_seen={s.last_seen or '?'}")
    return 0


def signals_run_export(args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import failure_signals, paths

    root = paths.resolve_root(args.dest)
    signals, by_id = signals__build(args, root)
    match = next((s for s in signals if s.name == args.name), None)
    if match is None:
        print(
            f"[commontrace] no signal named {args.name!r}. Run `commontrace signals list` "
            "for the exact names currently found.",
            file=sys.stderr,
        )
        return 1

    rows = (
        failure_signals.export_langsmith(match, by_id)
        if args.format == "langsmith"
        else failure_signals.export_braintrust(match, by_id)
    )
    text = "\n".join(json.dumps(r) for r in rows) + "\n"

    if args.out:
        try:
            safe_out = paths.safe_prepare_output_path(args.out)
        except (OSError, ValueError) as exc:
            print(f"[commontrace] could not write {args.out!r}: {exc}", file=sys.stderr)
            return 1
        with open(safe_out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"[commontrace] wrote {len(rows)} example(s) to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


# fleet operations
def fleet_add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "fleet",
        help="Run one experiment across many robots or edge agents: give them the same "
        "randomization, then pool their records.",
    )
    sub = p.add_subparsers(dest="fleet_cmd", required=True)

    ad = sub.add_parser("adopt", help="Copy SOURCE's experiment and gateway policy into a robot's store.")
    ad.add_argument("source", help="The store that started the experiment.")
    ad.add_argument("--dest", required=True, help="The robot's store.")
    ad.set_defaults(func=fleet_run_adopt)

    ck = sub.add_parser("check", help="Say whether these stores can be pooled, and what would not.")
    ck.add_argument("stores", nargs="+")
    ck.add_argument("--json", action="store_true")
    ck.set_defaults(func=fleet_run_check)

    mg = sub.add_parser("merge", help="Write one store holding every robot's records. Sources are untouched.")
    mg.add_argument("stores", nargs="+")
    mg.add_argument("--dest", required=True, help="An empty store to write.")
    mg.add_argument("--json", action="store_true")
    mg.set_defaults(func=fleet_run_merge)

    for operation in ("interventions", "operators", "ask-policy"):
        child = sub.add_parser(operation, help="Governed human corrections and bounded asking trials.")
        child.add_argument("request", nargs="?", default="{}")
        child.add_argument("--dest")
        child.set_defaults(func=fleet_run_human_help)

    world = sub.add_parser("worldmodel", help="Register, trust and sim-gate a robot world model.")
    actions = world.add_subparsers(dest="worldmodel_action", required=True)
    for action in ("register", "trust", "gate"):
        child = actions.add_parser(action)
        child.add_argument("request", nargs="?", default="{}")
        child.add_argument("--dest")
        child.set_defaults(func=fleet_run_worldmodel)

    signatures = sub.add_parser("signatures", help="Build, check and query force/proprioception profiles.")
    signature_actions = signatures.add_subparsers(dest="signature_action", required=True)
    for action in ("build", "check", "drift"):
        child = signature_actions.add_parser(action)
        child.add_argument("request", nargs="?", default="{}")
        child.add_argument("--dest")
        child.set_defaults(func=fleet_run_signatures)

    site = sub.add_parser(
        "site",
        help="Ingest bounded VDA state/order paths, inspect site layers and sync signed peer operations.",
    )
    site_actions = site.add_subparsers(dest="site_action", required=True)
    for action in ("ingest", "merge", "rules", "mesh"):
        child = site_actions.add_parser(action)
        child.add_argument("request", nargs="?", default="{}")
        child.add_argument("--dest")
        child.set_defaults(func=fleet_run_site)

    safety = sub.add_parser("safetycase", help="Build a living safety evidence graph and export scenarios.")
    safety_actions = safety.add_subparsers(dest="safetycase_action", required=True)
    for action in ("build", "claim", "argument", "evidence", "stale", "coverage", "export"):
        child = safety_actions.add_parser(action)
        child.add_argument("request", nargs="?", default="{}")
        child.add_argument("--dest")
        child.set_defaults(func=fleet_run_safetycase)

    shield = sub.add_parser("shield", help="Evaluate bounded STL monitors against a robot telemetry trace.")
    shield_actions = shield.add_subparsers(dest="shield_action", required=True)
    for action in ("spec", "check"):
        child = shield_actions.add_parser(action)
        child.add_argument("request", nargs="?", default="{}")
        child.add_argument("--dest")
        child.set_defaults(func=fleet_run_shield)


def fleet_run_worldmodel(args: argparse.Namespace) -> int:
    import json

    from commontrace import paths, world_model_trust

    body = json.loads(args.request)
    if not isinstance(body, dict):
        raise ValueError("world-model request must be an object")
    operations = {"register": world_model_trust.register, "trust": world_model_trust.trust,
                  "gate": world_model_trust.gate}
    try:
        result = operations[args.worldmodel_action](paths.resolve_root(args.dest), **body)
    except TypeError as exc:
        raise ValueError("world-model request fields do not match the operation") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def fleet_run_signatures(args: argparse.Namespace) -> int:
    import json

    from commontrace import paths
    from commontrace.robotics import signatures

    body = json.loads(args.request)
    if not isinstance(body, dict):
        raise ValueError("signature request must be a JSON object")
    operations = {"build": signatures.build, "check": signatures.check, "drift": signatures.drift}
    try:
        result = operations[args.signature_action](paths.resolve_root(args.dest), **body)
    except TypeError as exc:
        raise ValueError("signature request fields do not match the operation") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def fleet_run_site(args: argparse.Namespace) -> int:
    import json

    from commontrace import mesh_crdt, paths
    from commontrace.robotics import site_memory

    body = json.loads(args.request)
    if not isinstance(body, dict):
        raise ValueError("site-memory request must be a JSON object")
    root = paths.resolve_root(args.dest)
    if args.site_action == "ingest":
        message_type = body.pop("message_type", "state")
        if message_type == "order":
            result = site_memory.ingest_vda_order(root, **body)
        elif message_type == "zone":
            result = site_memory.ingest_vda_zone(root, **body)
        elif message_type == "state":
            result = site_memory.ingest_vda_state(root, **body)
        else:
            raise ValueError("site message_type must be state, zone or order")
    elif args.site_action == "merge":
        result = {"scene": site_memory.scene_graph(root, site=body["site"], context=body.get("context", [])),
                  "mesh": mesh_crdt.merge(root)}
    elif args.site_action == "rules":
        result = site_memory.rules(root, **body)
        if body.get("record_suggestion", False):
            result = {"rules": result, "suggestion": site_memory.suggest(root, site=body["site"], ruleset=result)}
    else:
        result = mesh_crdt.apply(root, body["operations"])
        result["view"] = mesh_crdt.merge(root)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def fleet_run_safetycase(args: argparse.Namespace) -> int:
    import json

    from commontrace import paths, safety_case

    body = json.loads(args.request)
    if not isinstance(body, dict):
        raise ValueError("safety-case request must be a JSON object")
    root = paths.resolve_root(args.dest)
    operations = {
        "build": safety_case.build, "claim": safety_case.add_claim, "argument": safety_case.add_argument,
        "evidence": safety_case.add_evidence, "stale": safety_case.mark_changed,
        "coverage": safety_case.coverage, "export": safety_case.export_scenario,
    }
    try:
        if args.safetycase_action == "export":
            result = operations[args.safetycase_action](root, body["scenario_id"])
        else:
            result = operations[args.safetycase_action](root, **body)
    except TypeError as exc:
        raise ValueError("safety-case request fields do not match the operation") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def fleet_run_shield(args: argparse.Namespace) -> int:
    import json

    from commontrace import paths
    from commontrace.robotics import shield

    body = json.loads(args.request)
    if not isinstance(body, dict):
        raise ValueError("shield request must be a JSON object")
    operation = shield.register if args.shield_action == "spec" else shield.check
    try:
        result = operation(paths.resolve_root(args.dest), **body)
    except TypeError as exc:
        raise ValueError("shield request fields do not match the operation") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def fleet_run_human_help(args: argparse.Namespace) -> int:
    import json

    from commontrace import human_help, paths
    from commontrace.commands.nextwave import request

    body = request(args.request)
    action = body.pop("action", "report")
    operations = {
        "interventions": {"report": human_help.interventions, "record": human_help.intervention},
        "operators": {"register": human_help.register_operator, "report": human_help.operator_quality,
                      "appeal": human_help.appeal, "review": human_help.review},
        "ask-policy": {"configure": human_help.configure_asking, "decide": human_help.should_ask,
                       "report": human_help.asking_report, "cost": human_help.record_asking_cost}}
    if args.fleet_cmd == "ask-policy" and action == "plan":
        result = human_help.plan(**body)
    else:
        if action not in operations[args.fleet_cmd]:
            raise ValueError("UniMem unsupported human-help action")
        result = operations[args.fleet_cmd][action](paths.resolve_root(args.dest), **body)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def _value_error_guard(fn):
    import sys

    def wrapped(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except ValueError as exc:
            print(f"[commontrace] error: {exc}", file=sys.stderr)
            return 2

    return wrapped


@_value_error_guard
def fleet_run_adopt(args: argparse.Namespace) -> int:
    from commontrace import fleet

    done = fleet.adopt(args.source, args.dest)
    print(
        f"[commontrace] {args.dest} now randomizes like {args.source}: salt {done['salt']!r}, "
        f"holdout {done['rate']:.0%}"
        + (f", environment {done['env']}" if done["env"] else "")
        + (f", protecting {', '.join(done['protected_prefixes'])}" if done["protected_prefixes"] else "")
    )
    return 0


def fleet__print_report(report: fleet.CheckReport) -> None:

    for s in report.stores:
        print(
            f"  {s.path}: {s.assignments:,} assignments, {s.outcomes:,} outcomes"
            + (f", {s.corrupt} unreadable line(s)" if s.corrupt else "")
            + f"  [salt {s.salt!r}, {s.rate:.0%}"
            + (f", {s.env}" if s.env else "")
            + "]"
        )
    for problem in report.problems:
        print(f"  CANNOT POOL: {problem}")
    for conflict in report.conflicts[:10]:
        print(f"  CONFLICT: {conflict}")
    if len(report.conflicts) > 10:
        print(f"  ... and {len(report.conflicts) - 10} more conflicts")
    if report.duplicates:
        print(f"  {report.duplicates} identical record(s) appear in more than one store and collapse to one")


@_value_error_guard
def fleet_run_check(args: argparse.Namespace) -> int:
    import json

    from commontrace import fleet

    report = fleet.check(args.stores)
    if args.json:
        import dataclasses

        print(json.dumps({**dataclasses.asdict(report), "mergeable": report.mergeable}, indent=2))
    else:
        fleet__print_report(report)
        print("mergeable" if report.mergeable else "NOT mergeable")
    return 0 if report.mergeable else 1


@_value_error_guard
def fleet_run_merge(args: argparse.Namespace) -> int:
    import json
    import sys

    from commontrace import fleet

    report = fleet.check(args.stores)
    if not report.mergeable:
        fleet__print_report(report)
        print("NOT merged", file=sys.stderr)
        return 1
    totals = fleet.merge(args.stores, args.dest, report=report)
    if args.json:
        print(json.dumps(totals, indent=2))
    else:
        print(
            f"[commontrace] merged {len(args.stores)} stores into {args.dest}: "
            f"{totals['assignments']:,} assignments, {totals['outcomes']:,} outcomes"
            + (f" ({totals['duplicates']} duplicate record(s) collapsed)" if totals["duplicates"] else "")
        )
        print(f"  commontrace experiment --dest {args.dest}   # or: commontrace proof status --dest {args.dest}")
    return 0


# function operations
def function_add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "function",
        help="Function kits: how CommonTrace measures support, sales, HR, coding, marketing, "
        "robotics, legal, finance, clinical -- or any function you define.",
    )
    sub = p.add_subparsers(dest="function_cmd", required=True)

    ls = sub.add_parser("list", help="The built-in kits.")
    ls.set_defaults(func=function_run_list)

    show = sub.add_parser("show", help="One kit's occasion, outcome model and planning assumptions.")
    show.add_argument("name", nargs="?", help="A kit key or a kit file. Default: this store's kit.")
    show.add_argument("--dest", default=None)
    show.set_defaults(func=function_run_show)

    fc = sub.add_parser("forecast", help="At your volume, how long until a verdict.")
    fc.add_argument("name", nargs="?", help="A kit key or a kit file. Default: this store's kit.")
    fc.add_argument("--daily", type=float, required=True, help="Occasions per day.")
    fc.add_argument("--baseline", type=float, default=None, help="Your current success rate.")
    fc.add_argument("--effect", type=float, default=None, help="Smallest effect worth detecting.")
    fc.add_argument("--rate", type=float, default=None, help="Holdout rate.")
    fc.add_argument(
        "--within",
        type=int,
        default=None,
        metavar="DAYS",
        help="Also say what daily volume a verdict within DAYS needs.",
    )
    fc.add_argument("--dest", default=None)
    fc.set_defaults(func=function_run_forecast)

    demo = sub.add_parser(
        "demo",
        help="Fill an EMPTY store with synthetic, labelled holdout data so a report "
        "can be shown before there is real data.",
    )
    demo.add_argument("name", nargs="?", help="A kit key or a kit file. Default: this store's kit.")
    demo.add_argument("--seed", type=int, default=0)
    demo.add_argument("--dest", default=None)
    demo.set_defaults(func=function_run_demo)

    pr = sub.add_parser("precision", help="Score the outcome detector against occasions a person has labelled.")
    pr.add_argument("sample", help="JSON lines: {occasion_id, truth, combine, signals}; see commontrace/precision.py.")
    pr.add_argument("--min-precision", type=float, default=0.9)
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=function_run_precision)

    chk = sub.add_parser("check", help="Validate a kit file.")
    chk.add_argument("file")
    chk.set_defaults(func=function_run_check)


def function__kit(args: argparse.Namespace):
    from commontrace import functions, paths

    if args.name:
        return functions.resolve(args.name)
    kit = functions.store_kit(paths.resolve_root(args.dest))
    if kit is None:
        raise functions.KitError(
            "this store has no function kit; name one (`commontrace function list`) or "
            "initialise with `commontrace init --function <name>`"
        )
    return kit


@_value_error_guard
def function_run_list(args: argparse.Namespace) -> int:
    from commontrace import functions

    for key, kit in sorted(functions.builtin_kits().items()):
        flag = "  (regulated)" if kit.regulated else ""
        print(f"{key:10s} {kit.title:26s} occasion: {kit.occasion_label:26s} wait {kit.outcome.window_days:g}d{flag}")
    return 0


@_value_error_guard
def function_run_show(args: argparse.Namespace) -> int:
    from commontrace import functions

    print(functions.render_kit(function__kit(args)))
    return 0


@_value_error_guard
def function_run_forecast(args: argparse.Namespace) -> int:
    from commontrace import functions

    print(
        functions.render_forecast(
            functions.forecast(
                function__kit(args),
                args.daily,
                baseline=args.baseline,
                effect=args.effect,
                rate=args.rate,
                within_days=args.within,
            )
        )
    )
    return 0


@_value_error_guard
def function_run_demo(args: argparse.Namespace) -> int:
    from commontrace import functions, paths

    root = paths.resolve_root(args.dest)
    kit = function__kit(args)
    paths.warn_if_implicit_cwd_store(args.dest)
    functions.seed_demo(root, kit, seed=args.seed)
    print(f"[commontrace] wrote {functions.DEMO_OCCASIONS} synthetic {kit.key} occasions to {root}")
    print(
        f"  Every occasion id starts DEMO-{kit.key}- and the experiment note says "
        f"'{functions.DEMO_NOTE}'. Three memories: one helps, one hurts, one does nothing."
    )
    print(f"  commontrace experiment --dest {root}")
    return 0


@_value_error_guard
def function_run_check(args: argparse.Namespace) -> int:
    from commontrace import functions

    kit = functions.load_file(args.file)
    print(
        f"ok: {kit.key} ({kit.title}), agent_type {kit.agent_type}, "
        f"occasion {kit.occasion_label}, wait {kit.outcome.window_days:g}d, "
        f"detectors {', '.join(kit.outcome.signals)} ({kit.outcome.combine})"
    )
    return 0


def function_run_precision(args: argparse.Namespace) -> int:
    import json
    import sys

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


COMMANDS = {
    "policy": policy_add_parser,
    "assurance": assurance_add_parser,
    "gate": gate_add_parser,
    "conformance": conformance_add_parser,
    "defense": defense_add_parser,
    "compression": compression_add_parser,
    "allocate": allocate_add_parser,
    "bill": bill_add_parser,
    "reliability": reliability_add_parser,
    "signals": signals_add_parser,
    "fleet": fleet_add_parser,
    "function": function_add_parser,
}
