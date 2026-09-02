"""`commontrace prove` — is the memory actually helping, and can you show it?

Two questions that sound the same and are not:

`prove outcomes` asks whether your fleet's recorded numbers have moved
since its baseline window. That is a real question with a real answer, and
it is **observational**: a model upgrade or a shift in your task mix sits
inside the same window, so the comparison cannot separate them from
anything this product did. It says so on every response.

`prove assign` / `prove record` run a **randomized holdout**, which can.
Some fraction of eligible memory is deliberately withheld, so your fleet
generates its own control arm; the comparison is then two arms of the same
fleet in the same window, differing only by whether the memory was
injected. That is what makes it survive "what else changed that quarter?".

The holdout is the loop an agent runs, not something a person types:
before injecting retrieved memory, ask `assign` which of it to use; after
the task, `record` how it went. Both are here as commands so the loop can
be driven from a shell script or a Makefile without writing MCP calls by
hand, and so it can be exercised once by a human before being wired into a
fleet.

An operator has to start an experiment first
(`python -m hub.manage start-experiment <org_id> <rate>`); until then
`assign` reports that none is running rather than silently injecting
everything.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from commontrace import hub_client
from commontrace.commands import _format

_resolve_hub = _format.resolve_hub


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "prove",
        help="Is the memory helping? Observed change, and the randomized holdout that shows cause.",
    )
    sub = p.add_subparsers(dest="prove_cmd", required=True)

    outcomes = sub.add_parser(
        "outcomes",
        help="Has your fleet's recorded performance changed, and does the holdout say why?",
    )
    outcomes.add_argument(
        "--agent-type", default="", help="Narrow to one agent type (default: the whole fleet)."
    )
    outcomes.add_argument("--json", action="store_true", help="Raw JSON instead of a report.")
    outcomes.add_argument("--hub-url", default=None, help="Default: $COMMONTRACE_HUB_URL")
    outcomes.add_argument("--hub-api-key", default=None, help="Default: $COMMONTRACE_HUB_API_KEY")
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


def _validity_lines(report: dict) -> list[str]:
    """The validity verdict, placed ABOVE the effect sizes.

    `prove` is the document that goes into a renewal conversation, which is
    the worst possible place for a caveat under a table: the number gets
    quoted and the footnote does not travel with it. When the sample cannot
    support the estimate, that is the first thing on this section -- and the
    effects that follow are labelled, not silently printed.
    """
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


def _pct(value) -> str:
    return f"{value:.0%}" if isinstance(value, (int, float)) else "-"


def render_outcomes(report: dict) -> str:
    lines = ["# Is the memory helping?", ""]
    causal = report.get("causal") or {}
    effects = causal.get("effects") or []

    # The causal answer FIRST when there is one. It is the stronger claim,
    # and burying it under the observational table invites a reader to
    # quote the weaker number because they saw it first.
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
        print(f"[commontrace] {exc}", file=sys.stderr)
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
        print(f"[commontrace] {exc}", file=sys.stderr)
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
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    n = result.get("observations_resolved", 0)
    print(f"occasion {result['occasion_id']}: {n} observation(s) resolved.")
    if n == 0:
        # Not an error: the common causes are a already-reported occasion
        # (only the first report counts, deliberately) and an occasion that
        # never had an assignment. Both are worth naming rather than
        # leaving a bare zero.
        print(
            "  Nothing to resolve. Either this occasion was already reported (only the "
            "first report counts, so a result already counted cannot be flipped), or "
            "`prove assign` was never called for it."
        )
    return 0
