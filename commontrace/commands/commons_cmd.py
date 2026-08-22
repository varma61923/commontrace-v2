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

from commontrace import failure_import, hub_client, overlap, paths, trace_io

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
    sign.add_argument(
        "--from", dest="from_file", default=None, metavar="FILE",
        help="Sign failures from a file you ALREADY have -- an incident export, a "
        "postmortem index, a pasted column of alert titles. JSONL, JSON, CSV/TSV "
        "or one failure per line. Use this to get a coverage number without "
        "adopting CommonTrace first.",
    )
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
    rep.add_argument(
        "--from", dest="from_file", default=None, metavar="FILE",
        help="Failures you already have, in any of the formats `commons sign --from` "
        "accepts. Signed locally; no failure text is sent.",
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

    contrib = sub.add_parser(
        "contribute",
        help="Review and bulk-share a selected set of your Hub traces. "
        "Previews by default; requires --confirm to actually share.",
    )
    contrib.add_argument(
        "--tags", default="",
        help="Comma-separated tags. Only traces carrying one of these are considered.",
    )
    contrib.add_argument("--query", default="", help="Full-text filter on your own traces.")
    contrib.add_argument(
        "--limit", type=int, default=50,
        help="Cap on how many traces to consider in one pass (default 50).",
    )
    contrib.add_argument(
        "--rationale", default="",
        help="Why this batch is substrate rather than business logic. Recorded per trace.",
    )
    contrib.add_argument(
        "--confirm", action="store_true",
        help="Actually share. Without this the command only shows what WOULD be shared.",
    )
    contrib.add_argument("--hub-url", default=None)
    contrib.add_argument("--hub-api-key", default=None)
    contrib.set_defaults(func=run_contribute)

    usage = sub.add_parser(
        "usage",
        help="What your plan entitles you to this period, and how much "
        "allowance you have EARNED by contributing.",
    )
    usage.add_argument("--hub-url", default=None)
    usage.add_argument("--hub-api-key", default=None)
    usage.set_defaults(func=run_usage)


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


def signatures_from_file(path: str) -> tuple[list[dict], dict]:
    """Sign failures a fleet already has, identically to build_signatures().

    Same `overlap.minhash` over the same title+text+tags concatenation, so a
    signature produced from an incident export is comparable to one produced
    from a `memory/traces/` store and to a Hub trace. If these ever diverge
    the numbers stay plausible and become meaningless, which is the worst
    failure mode available here -- hence one shared code path for the text.
    """
    failures, stats = failure_import.read_failures(path)
    signed = []
    for f in failures:
        text = " ".join([f["label"], f["text"], " ".join(f["tags"])])
        signed.append({
            "label": f["label"],
            "signature": overlap.minhash(text, COMMONS_NUM_PERM),
        })
    return signed, stats


def _describe_import(stats: dict) -> None:
    """Say what was actually measured. A coverage fraction computed over 400
    copies of one alert describes that alert, not the fleet, so a collapse
    or a cap has to be visible next to the number it changed."""
    print(f"  read {stats['rows']} row(s) as {stats['format']}; "
          f"{stats['unique']} distinct failure(s).")
    if stats["deduplicated"]:
        print(f"  collapsed {stats['deduplicated']} exact duplicate(s) -- otherwise one "
              "noisy alert would dominate the number.")
    if stats["truncated"]:
        print(f"  NOTE: capped at {failure_import.MAX_FAILURES}; "
              f"{stats['truncated']} failure(s) not measured.", file=sys.stderr)


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
    stats = None
    if getattr(args, "from_file", None):
        try:
            failures, stats = signatures_from_file(args.from_file)
        except failure_import.FailureImportError as exc:
            print(f"[commontrace] {exc}", file=sys.stderr)
            return 1
    else:
        root = paths.resolve_root(args.dest)
        failures = build_signatures(root)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"num_perm": COMMONS_NUM_PERM, "failures": failures}, fh, indent=2)

    print(f"[commontrace] wrote {args.out}")
    if stats:
        _describe_import(stats)
    print(f"  {len(failures)} recurring failure(s), as MinHash signatures only.")
    print("  Failure text is NOT in this file and cannot be reconstructed from it.")
    if stats:
        # The labels DO travel, and for an imported file they are the
        # prospect's own incident titles rather than opaque trace ids. Saying
        # so here rather than in a doc, because this is the moment someone
        # decides whether to send the file.
        print("  Labels (your incident titles) ARE in this file and are echoed back "
              "in the report.")
        print("  Review it before sending if those titles are themselves sensitive.")
        return 0
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
    if args.signatures and getattr(args, "from_file", None):
        print("[commontrace] pass --signatures or --from, not both: they are two ways "
              "of supplying the same input.", file=sys.stderr)
        return 1

    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved

    if getattr(args, "from_file", None):
        try:
            failures, stats = signatures_from_file(args.from_file)
        except failure_import.FailureImportError as exc:
            print(f"[commontrace] {exc}", file=sys.stderr)
            return 1
        if not args.json:
            print(f"[commontrace] signing {args.from_file} locally -- "
                  "no failure text leaves this machine.")
            _describe_import(stats)
            print()
    elif args.signatures:
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


def run_contribute(args: argparse.Namespace) -> int:
    """Bulk-share a selected batch, with a mandatory preview step.

    Deliberately NOT a classifier. Deciding what is substrate and what is
    proprietary is a judgment call with asymmetric cost -- over-sharing is
    irreversible in the way that matters (another org may already have
    copied it), while under-sharing costs nothing but a second pass. So the
    operator states the selection, sees exactly what it resolved to, and
    has to say --confirm. Nothing is inferred and nothing is shared by
    default.
    """
    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    if not tags and not args.query:
        print(
            "[commontrace] refusing to consider every trace you own.\n"
            "  Narrow the batch with --tags and/or --query. Sharing is\n"
            "  effectively publication, so the selection has to be deliberate.",
            file=sys.stderr,
        )
        return 1

    limit = max(1, min(int(args.limit), 200))
    try:
        found = asyncio.run(
            hub_client._call_tool(
                hub_url, api_key, "search_traces",
                {"query": args.query, "tags": tags, "limit": limit},
            )
        )
    except (hub_client.HubClientUnavailable, hub_client.HubConnectionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    if found.get("error"):
        print(f"[commontrace] search_traces failed: {found['error']}", file=sys.stderr)
        return 1

    traces = found.get("traces") or []
    already = [t for t in traces if t.get("shared_with_commons")]
    candidates = [t for t in traces if not t.get("shared_with_commons")]

    if not candidates:
        print("[commontrace] nothing new to share for that selection.")
        if already:
            print(f"  ({len(already)} matching trace(s) are already in the commons.)")
        return 0

    print(f"[commontrace] {len(candidates)} trace(s) selected for the commons:\n")
    for t in candidates:
        tag_str = ", ".join(t.get("tags") or []) or "(no tags)"
        print(f"  {t['id'][:8]}  {str(t.get('title', ''))[:64]}")
        print(f"            tags: {tag_str}")
    if found.get("has_more"):
        print(f"\n  (more matches exist beyond --limit {limit}; re-run to continue)")

    if not args.confirm:
        print(
            "\n  PREVIEW ONLY -- nothing has been shared.\n"
            "  Read the list above carefully. A shared trace's full content (title,\n"
            "  context, solution) can be returned to another org whose failure matches\n"
            "  it. Withdrawal stops future matches but cannot retract what someone has\n"
            "  already retrieved. Share substrate, never business logic.\n"
            "\n  Re-run with --confirm to share these."
        )
        return 0

    shared, failed = 0, 0
    for t in candidates:
        try:
            asyncio.run(hub_client.share_trace(hub_url, api_key, t["id"], args.rationale))
            shared += 1
        except (hub_client.HubClientUnavailable, hub_client.HubConnectionError) as exc:
            failed += 1
            print(f"[commontrace] could not share {t['id'][:8]}: {exc}", file=sys.stderr)

    print(f"\n[commontrace] shared {shared} trace(s) with the commons.")
    if failed:
        print(f"  {failed} failed -- see errors above. Re-running is safe: "
              "already-shared traces are skipped.", file=sys.stderr)
    print("  Withdraw any of them with: commontrace commons unshare <trace_id>")
    return 1 if failed else 0


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


def run_usage(args: argparse.Namespace) -> int:
    """Show the meter.

    Prints `earned` separately from `granted` on purpose: the difference is
    the entire argument for contributing. An org that can see it is ahead
    on credit has a reason to keep sharing; an org that cannot is being
    asked for a favour.
    """
    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved
    try:
        r = asyncio.run(hub_client.account_usage(hub_url, api_key))
    except (hub_client.HubClientUnavailable, hub_client.HubConnectionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    q = r["commons_queries"]
    unlimited = -1

    def fmt(n):
        return "unlimited" if n == unlimited else f"{n:,}"

    print(f"[commontrace] plan: {r['plan']}   billing period {r['period']} (UTC)")
    print(f"  traces stored:    {r['traces']['used']:,} of {fmt(r['traces']['limit'])}")
    print(f"  commons queries:  {q['used']:,} of {fmt(q['allowance'])}"
          f"   ({fmt(q['remaining'])} remaining)")
    print(f"    granted by plan:  {fmt(q['granted'])}")
    print(f"    earned by contributing: {q['earned']:,}")
    print()
    if r["delivered_hits"]:
        print(f"  Your shared traces have covered another fleet's failure "
              f"{r['delivered_hits']:,} time(s).")
        print("  That is what earned the allowance above -- it is not a discount "
              "anyone negotiated.")
    else:
        print("  You have not delivered any commons hits yet. Sharing traces that "
              "cover other")
        print("  fleets' failures earns query allowance directly: "
              "`commontrace commons contribute --tags <...>`.")
    return 0
