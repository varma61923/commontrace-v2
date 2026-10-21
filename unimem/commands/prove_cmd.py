"""`unimem prove` — is the memory actually helping, and can you show it?"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Callable

from unimem import hub_client
from unimem.commands import _format

_resolve_hub = _format.resolve_hub


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "prove",
        help="Is the memory helping? Observed change, and the randomized holdout that shows cause.",
    )
    sub = p.add_subparsers(dest="prove_cmd", required=True)
    oblivious = sub.add_parser("recall-oblivious", help="Run reference two-server XOR-PIR on a local fixture.")
    oblivious.add_argument("request", help="JSON file containing records, centroids and query vector.")
    oblivious.add_argument("--clearance", required=True)
    oblivious.add_argument("--limit", type=int, default=5)
    oblivious.add_argument("--clusters", type=int, default=None)
    oblivious.set_defaults(func=run_oblivious)
    measurements = sub.add_parser("measurements", help="Register and list audited attested worker builds.")
    operations = measurements.add_subparsers(dest="measurement_operation", required=True)
    for operation in ("register", "list"):
        child = operations.add_parser(operation)
        child.add_argument("request", nargs="?", default="{}")
        child.add_argument("--dest")
        child.set_defaults(func=run_measurements)
    effect = sub.add_parser("effect", help="Commit and prove complete fixed-point effect cohorts.")
    effect.add_argument("request", nargs="?", default="{}",
                        help="JSON action: register, challenge, assign, observe, prove.")
    effect.add_argument("--dest")
    effect.set_defaults(func=run_effect)
    verify = sub.add_parser("verify", help="Verify an effect statement against an independently pinned challenge.")
    verify.add_argument("request", nargs="?", default="{}")
    verify.add_argument("--witness-file", help="Explicit owner disclosure for transparent recomputation (max 16 MiB).")
    verify.add_argument("--dest")
    verify.set_defaults(func=run_effect_verify)
    policy = sub.add_parser("policy", help="Prove and review changes to finite memory permissions.")
    operations = policy.add_subparsers(dest="policy_operation", required=True)
    for operation in ("current", "diff", "certify", "review", "activate"):
        child = operations.add_parser(operation)
        child.add_argument("request", nargs="?", default="{}", help="JSON object or request file.")
        child.add_argument("--dest")
        child.set_defaults(func=run_policy)

    outcomes = sub.add_parser(
        "outcomes",
        help="Has your fleet's recorded performance changed, and does the holdout say why?",
    )
    outcomes.add_argument(
        "--agent-type", default="", help="Narrow to one agent type (default: the whole fleet)."
    )
    outcomes.add_argument("--json", action="store_true", help="Raw JSON instead of a report.")
    outcomes.add_argument("--hub-url", default=None, help="Default: $UNIMEM_HUB_URL")
    outcomes.add_argument("--hub-api-key", default=None, help="Default: $UNIMEM_HUB_API_KEY")
    outcomes.set_defaults(func=run_outcomes)

    assign = sub.add_parser(
        "assign",
        help="Which of these traces to inject on this occasion, and which to withhold.",
    )
    assign.add_argument("occasion_id", help="Your own id for one unit of work.")
    assign.add_argument(
        "trace_ids", nargs="+", help="The traces eligible on this occasion."
    )
    assign.add_argument("--json", action="store_true")
    assign.add_argument("--hub-url", default=None)
    assign.add_argument("--hub-api-key", default=None)
    assign.set_defaults(func=run_assign)

    record = sub.add_parser(
        "record", help="Report how an occasion went, resolving both holdout arms."
    )
    record.add_argument("occasion_id")
    outcome = record.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--succeeded", action="store_true", help="The task succeeded.")
    outcome.add_argument("--failed", action="store_true", help="The task did not succeed.")
    record.add_argument("--hub-url", default=None)
    record.add_argument("--hub-api-key", default=None)
    record.set_defaults(func=run_record)


def run_oblivious(args: argparse.Namespace) -> int:
    from unimem import oblivious

    with open(args.request, encoding="utf-8") as stream:
        body = json.load(stream)
    if not isinstance(body, dict):
        raise ValueError("oblivious recall request must be a JSON object")
    centroids = body["centroids"]
    block_bytes = int(body.get("block_bytes", 4096))
    labels = body.get("labels", ("public", "internal", "confidential", "restricted"))
    database = oblivious.cluster_records(body["records"], centroids, clearance=args.clearance,
                                         labels=labels, block_bytes=block_bytes)
    first = oblivious.XorPIRReplica(database, block_bytes=block_bytes)
    second = oblivious.XorPIRReplica(database, block_bytes=block_bytes)

    def fetch(cluster, query0, query1):
        if query0 is None:
            return first.handle(cluster, None)
        return first.handle_many(cluster, query0), second.handle_many(cluster, query1)

    result = oblivious.client_recall(centroids, body["query_vector"], clearance=args.clearance,
                                     limit=args.limit, clusters_to_fetch=args.clusters, fetch_pair=fetch,
                                     labels=labels)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def run_measurements(args: argparse.Namespace) -> int:
    from unimem import attested_keys, paths
    from unimem.commands.nextwave import request

    body = request(args.request)
    root = paths.resolve_root(args.dest)
    if args.measurement_operation == "register":
        result: Any = attested_keys.register(root, **body)
    else:
        if body:
            raise ValueError("UniMem measurement listing takes no request fields")
        result = {"measurements": attested_keys.measurements(root)}
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def run_effect(args: argparse.Namespace) -> int:
    from unimem import paths, zk_effect_io
    from unimem.commands.nextwave import request

    body = request(args.request)
    action = body.pop("action", "prove")
    operations: dict[str, Callable[..., Any]] = {"register": zk_effect_io.register, "challenge": zk_effect_io.challenge,
        "assign": zk_effect_io.assign, "observe": zk_effect_io.observe, "prove": zk_effect_io.prove,
        "configure-prover": zk_effect_io.configure_prover, "witness": zk_effect_io.export_witness}
    if action not in operations:
        raise ValueError("UniMem unsupported effect proof action")
    try:
        result = operations[action](paths.resolve_root(args.dest), **body)
    except TypeError as exc:
        raise ValueError("UniMem effect proof fields do not match the requested action") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def run_effect_verify(args: argparse.Namespace) -> int:
    from pathlib import Path

    from unimem import nextwave_store, paths, zk_effect_io
    from unimem.commands.nextwave import request

    body = request(args.request)
    if args.witness_file:
        nextwave_store.operator()
        if "witness" in body:
            raise ValueError("UniMem choose a witness object or --witness-file")
        with Path(args.witness_file).open("rb") as stream:
            raw = stream.read(16*1024*1024+1)
        if len(raw) > 16*1024*1024:
            raise ValueError("UniMem proof witness file exceeds 16 MiB")
        body["witness"] = json.loads(raw)
    try:
        result = zk_effect_io.verify(paths.resolve_root(args.dest), **body)
    except TypeError as exc:
        raise ValueError("UniMem proof verification fields do not match the requested profile") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["verified"] else 1


def run_policy(args: argparse.Namespace) -> int:
    from unimem import paths, policy_io
    from unimem.commands.nextwave import request

    operations: dict[str, Callable[..., Any]] = {
        "current": policy_io.current, "diff": policy_io.diff, "certify": policy_io.certify,
        "review": policy_io.review, "activate": policy_io.activate}
    try:
        result = operations[args.policy_operation](paths.resolve_root(args.dest), **request(args.request))
    except TypeError as exc:
        raise ValueError("UniMem policy request fields do not match the operation") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def _validity_lines(report: dict[str, Any]) -> list[str]:
    if not report:
        return []
    verdict = report.get("verdict", "")
    if verdict == "COMPROMISED":
        lines = [
            "> **Do not quote the effect sizes in this section.** The comparison "
            "below was computed on a sample that a named mechanism is biasing, so "
            "it is not an estimate of the causal effect. This is not a sample-size "
            "problem and more data will not fix it.",
            "",
        ]
        for f in report.get("findings", []):
            if f.get("severity") == "INVALIDATES":
                lines.append(f"> - {f.get('headline', '')} {f.get('detail', '')}".rstrip())
        return lines + [""]
    if verdict == "WEAKENED":
        weak = [f for f in report.get("findings", []) if f.get("severity") == "WEAKENS"]
        return [
            "> **Weakened.** Still an estimate, with less behind it than its "
            "confidence interval implies. "
            + " ".join(f.get("headline", "") for f in weak),
            "",
        ]
    return [
        "_Validity checked: attrition, arm balance, mid-run re-randomization, "
        "conflicting arms, outcome variation. Nothing found. Not checkable here: "
        "whether an agent used a memory it was told to withhold._",
        "",
    ]


def _pct(value: Any) -> str:
    return f"{value:.0%}" if isinstance(value, (int, float)) else "-"


def render_outcomes(report: dict[str, Any]) -> str:
    lines = ["# Is the memory helping?", ""]
    causal = report.get("causal") or {}
    effects = causal.get("effects") or []

    if effects:
        lines += [
            "## Caused by the memory (randomized holdout)",
            "",
            f"{causal.get('n_observations', 0):,} resolved observation(s) across "
            f"{causal.get('n_occasions', 0):,} occasion(s), "
            f"holdout rate {_pct(causal.get('holdout_rate'))}.",
            "",
        ]
        lines += _validity_lines(causal.get("integrity") or {})
        for e in effects:
            lines.append(f"### {e['verdict']} — {e.get('title', e['trace_id'])}")
            lines.append("")
            lines.append(
                f"- injected {_pct(e['rate_injected'])} (n={e['n_injected']}) vs "
                f"withheld {_pct(e['rate_withheld'])} (n={e['n_withheld']}), "
                f"effect **{e['effect']:+.1%}**"
            )
            if e["verdict"] in ("HELPS", "HURTS"):
                lines.append(
                    f"- 95% CI [{e['ci_95'][0]:+.1%}, {e['ci_95'][1]:+.1%}], p={e['p_value']:.4f}"
                )
            if e.get("note"):
                lines.append(f"- {e['note']}")
            lines.append("")
    elif causal.get("experiment_running"):
        lines += [
            "## Caused by the memory (randomized holdout)",
            "",
            "An experiment is running but nothing has been measured yet. Agents must "
            "call `prove assign` before injecting and `prove record` afterwards.",
            "",
        ]
    else:
        lines += [
            "## Caused by the memory (randomized holdout)",
            "",
            "No experiment is running, so nothing below is causal. Ask your operator to "
            "start one (`hub.manage start-experiment <org_id> <rate>`) — it is the only "
            "thing here that can separate this product's effect from everything else "
            "that changed in the same window.",
            "",
        ]

    lines += ["## Observed change since your baseline window", "", report.get("headline", ""), ""]
    rows = report.get("metrics") or []
    if rows:
        lines += ["| metric | baseline | now | change | |", "|---|---:|---:|---:|---|"]
        for r in rows:
            b, c = r["baseline"], r["current"]
            delta = f"{r['delta']:+.1%}" if r.get("delta") is not None else "-"
            lines.append(
                f"| {r['metric']} | {_pct(b['rate'])} (n={b['n']}) | "
                f"{_pct(c['rate'])} (n={c['n']}) | {delta} | {r['verdict']} |"
            )
        lines.append("")
    if report.get("caveat"):
        lines += [f"> {report['caveat']}", ""]
    return "\n".join(lines)


def run_outcomes(args: argparse.Namespace) -> int:
    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved
    try:
        report = asyncio.run(
            hub_client.fleet_outcomes(hub_url, api_key, agent_type=args.agent_type)
        )
    except hub_client.HubConnectionError as exc:
        print(f"[unimem] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2) if args.json else render_outcomes(report))
    return 0


def run_assign(args: argparse.Namespace) -> int:
    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved
    try:
        result = asyncio.run(
            hub_client.holdout_assign(hub_url, api_key, args.trace_ids, args.occasion_id)
        )
    except hub_client.HubConnectionError as exc:
        print(f"[unimem] {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"occasion {result['occasion_id']}  (holdout rate {_pct(result.get('holdout_rate'))})")
    for trace_id in result.get("inject", []):
        print(f"  INJECT   {trace_id}")
    for trace_id in result.get("withhold", []):
        print(f"  WITHHOLD {trace_id}")
    if not result.get("inject") and not result.get("withhold"):
        print("  (no eligible traces -- none of those ids belong to this org)")
    print(f"\n{result.get('note', '')}")
    return 0


def run_record(args: argparse.Namespace) -> int:
    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved
    try:
        result = asyncio.run(
            hub_client.record_occasion_outcome(
                hub_url, api_key, args.occasion_id, succeeded=args.succeeded
            )
        )
    except hub_client.HubConnectionError as exc:
        print(f"[unimem] {exc}", file=sys.stderr)
        return 1

    n = result.get("observations_resolved", 0)
    print(f"occasion {result['occasion_id']}: {n} observation(s) resolved.")
    if n == 0:
        print(
            "  Nothing to resolve. Either this occasion was already reported (only the "
            "first report counts, so a result already counted cannot be flipped), or "
            "`prove assign` was never called for it."
        )
    return 0
