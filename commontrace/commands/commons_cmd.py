"""`commontrace commons` — the self-serve cross-org coverage question.

    Of the failures my fleet keeps hitting, what fraction has some other
    fleet already solved?

This is the number STRATEGY.md §5 says the cross-org thesis lives or dies
on, and the point of this command is that answering it takes one call and
requires contributing nothing first. `commontrace overlap` can answer the
same question, but only bilaterally: both fleets export signature files and
somebody exchanges them by hand. That is a research instrument. This is the
product version -- ask the Hub, get the number.

No failure text is sent. `sign` MinHashes locally and only signatures leave
this machine; what comes back is drawn only from traces whose owners
explicitly shared them.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys

from commontrace import hub_client, overlap, paths, trace_io

# Kept in step with hub/commons.py's signing. Both sides sign a trace on
# title + context + tags -- the *situation*, not the fix -- so the
# comparison is symmetric. A change here without the matching change there
# silently degrades every similarity score rather than failing.
COMMONS_NUM_PERM = overlap.DEFAULT_NUM_PERM


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "commons",
        help="Ask the Hub how many of your recurring failures another fleet has already solved.",
    )
    sub = p.add_subparsers(dest="commons_cmd", required=True)

    sign = sub.add_parser(
        "sign",
        help="Write this store's recurring failures out as signatures only (no text).",
    )
    sign.add_argument("--out", required=True, help="Where to write the signature JSON.")
    sign.add_argument("--dest", default=None)
    sign.set_defaults(func=run_sign)

    rep = sub.add_parser(
        "report",
        help="Ask the Hub's commons how much of your failure set it already covers.",
    )
    rep.add_argument(
        "--signatures", default=None,
        help="A file from `commons sign`. Omit to sign this store on the fly.",
    )
    rep.add_argument("--threshold", type=float, default=None)
    rep.add_argument(
        "--counts-only", action="store_true",
        help="Ask for the headline number without pulling any matched content.",
    )
    rep.add_argument("--json", action="store_true", help="Emit raw JSON instead of markdown.")
    rep.add_argument("--hub-url", default=None, help="Default: $COMMONTRACE_HUB_URL")
    rep.add_argument("--hub-api-key", default=None, help="Default: $COMMONTRACE_HUB_API_KEY")
    rep.add_argument("--dest", default=None)
    rep.set_defaults(func=run_report)

    share = sub.add_parser(
        "share",
        help="Contribute one of your Hub traces to the commons (opt-in, revocable).",
    )
    share.add_argument("trace_id")
    share.add_argument(
        "--rationale", default="",
        help="Why this trace is substrate rather than business logic. Recorded for audit.",
    )
    share.add_argument("--hub-url", default=None)
    share.add_argument("--hub-api-key", default=None)
    share.set_defaults(func=run_share)

    unshare = sub.add_parser("unshare", help="Withdraw one of your traces from the commons.")
    unshare.add_argument("trace_id")
    unshare.add_argument("--hub-url", default=None)
    unshare.add_argument("--hub-api-key", default=None)
    unshare.set_defaults(func=run_unshare)


def _safe_tags(raw: object) -> list[str]:
    return [str(t) for t in raw if t is not None] if isinstance(raw, (list, tuple)) else []


def _recurring_failures(root: str) -> list[dict]:
    """Traces whose outcome recorded a repeated error -- the failures a
    fleet keeps paying for, which is exactly what the commons might cover."""
    out = []
    tdir = paths.traces_dir(root)
    for path in sorted(glob.glob(os.path.join(tdir, "*.md"))):
        if os.path.basename(path) == "README.md":
            continue
        instance, _ = trace_io.read(path)
        outcome = instance.get("outcome")
        if isinstance(outcome, dict) and outcome.get("repeated_error") is True:
            out.append(instance)
    return out


def build_signatures(root: str) -> list[dict]:
    """Reduce this store's recurring failures to label+signature pairs.

    Signed on title + context + tags to match hub/commons.py exactly. Labels
    are local trace-id prefixes: they are echoed back in the report so a
    result can be traced to a file, and they never leave without the
    operator running this command.
    """
    failures = []
    for tr in _recurring_failures(root):
        tags = _safe_tags(tr.get("tags"))
        text = " ".join([
            str(tr.get("title") or ""), str(tr.get("context_text") or ""), " ".join(tags),
        ])
        failures.append({
            "label": str(tr.get("id") or "")[:12] or "failure",
            "signature": overlap.minhash(text, COMMONS_NUM_PERM),
        })
    return failures


def _resolve_hub(args) -> tuple[str, str] | None:
    hub_url = args.hub_url or os.environ.get("COMMONTRACE_HUB_URL")
    api_key = args.hub_api_key or os.environ.get("COMMONTRACE_HUB_API_KEY")
    if not hub_url or not api_key:
        print(
            "[commontrace] a Hub URL and API key are required.\n"
            "  Set COMMONTRACE_HUB_URL and COMMONTRACE_HUB_API_KEY, or pass\n"
            "  --hub-url / --hub-api-key.",
            file=sys.stderr,
        )
        return None
    return hub_url, api_key


def run_sign(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    failures = build_signatures(root)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"num_perm": COMMONS_NUM_PERM, "failures": failures}, fh, indent=2)

    print(f"[commontrace] wrote {args.out}")
    print(f"  {len(failures)} recurring failure(s), as MinHash signatures only.")
    print("  Failure text is NOT in this file and cannot be reconstructed from it.")
    if not failures:
        print(
            "  NOTE: no recurring failures found (traces with outcome.repeated_error=true).\n"
            "  Capture them with `commontrace capture --repeated-error` for this to\n"
            "  have anything to measure.",
            file=sys.stderr,
        )
    return 0


def _render(report: dict) -> str:
    n_f = report["n_failures"]
    n_cov = report["n_covered"]
    frac = report["covered_fraction"]
    lines = [
        "# Commons Coverage Report",
        "",
        f"**{n_cov} of {n_f}** of your recurring failures "
        f"(**{frac:.0%}**) have already been solved by another fleet.",
        "",
        f"- Commons corpus searched: {report['n_commons_traces']} shared trace(s) from other orgs",
        f"- Match threshold: {report['threshold']}",
        "",
    ]
    if report.get("note"):
        lines += [f"> {report['note']}", ""]

    by_type = report.get("by_agent_type") or {}
    if by_type:
        lines += ["## Covered by agent type", "", "| Agent type | Covered |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in by_type.items()]
        lines.append("")

    matches = report.get("matches") or []
    if matches:
        lines += ["## What the commons already knows", ""]
        for m in matches:
            trace = m.get("trace") or {}
            lines += [
                f"### {trace.get('title', '(untitled)')}",
                "",
                f"- Matches your `{m['failure_label']}` at similarity **{m['similarity']}**",
                f"- Tags: {', '.join(m.get('tags') or []) or '(none)'}",
                "",
                f"**Solution:** {trace.get('solution_text', '')}",
                "",
            ]
    return "\n".join(lines)


def run_report(args: argparse.Namespace) -> int:
    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved

    if args.signatures:
        try:
            with open(args.signatures, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"[commontrace] cannot read {args.signatures}: {exc}", file=sys.stderr)
            return 1
        failures = payload.get("failures") if isinstance(payload, dict) else None
        if not isinstance(failures, list):
            print(
                f"[commontrace] {args.signatures} is not a `commons sign` file "
                "(expected a 'failures' list).",
                file=sys.stderr,
            )
            return 1
    else:
        failures = build_signatures(paths.resolve_root(args.dest))

    try:
        report = asyncio.run(
            hub_client.commons_overlap(
                hub_url, api_key, failures,
                threshold=args.threshold, include_matches=not args.counts_only,
            )
        )
    except (hub_client.HubClientUnavailable, hub_client.HubConnectionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2) if args.json else _render(report))
    return 0


def run_share(args: argparse.Namespace) -> int:
    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved
    try:
        result = asyncio.run(
            hub_client.share_trace(hub_url, api_key, args.trace_id, args.rationale)
        )
    except (hub_client.HubClientUnavailable, hub_client.HubConnectionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    print(f"[commontrace] shared {result['id']} with the commons.")
    print("  Other orgs whose failures match it can now see this trace in full.")
    print(f"  Withdraw it any time: commontrace commons unshare {result['id']}")
    return 0


def run_unshare(args: argparse.Namespace) -> int:
    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved
    try:
        result = asyncio.run(hub_client.unshare_trace(hub_url, api_key, args.trace_id))
    except (hub_client.HubClientUnavailable, hub_client.HubConnectionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    print(f"[commontrace] withdrew {result['id']} from the commons.")
    return 0
