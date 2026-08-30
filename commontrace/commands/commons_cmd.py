"""`commontrace commons` — consult, and optionally propose to, the
CommonTrace Knowledge Base.

    Of the failures my fleet keeps hitting, what fraction does the
    Knowledge Base already solve? And: has anyone already written down
    the answer to this ONE failure?

This is not org-to-org sharing: the Knowledge Base is a single corpus the
operator authors and curates (substrate knowledge -- protocol semantics,
vendor documentation, standards, and accepted community submissions --
never another customer's own trace), the way a team consults Stack
Overflow or an internal wiki, not the way it would consult a competitor's
support queue. `commons_access` (your plan) is the "optional" part:
whether you consult it at all.

`submit` lets you propose an entry -- like posting a Stack Overflow
answer, not sharing your own incident history. Nothing is published by
that call: an operator reviews it, and only an accepted submission ever
becomes visible to anyone else, at which point it raises your Knowledge
Base query allowance. `submissions` checks status. A pending or rejected
submission is never visible to any other org, and is never added to your
fleet's own coverage numbers either.

No failure text is sent for `sign`/`report`/`ask`. `sign` MinHashes
locally and only signatures leave this machine; what comes back is drawn
only from the Knowledge Base's curated entries. `submit` is different by
necessity -- proposing an entry means sending its actual text, since an
operator has to read it to review it.

(If you specifically want a bilateral, fully-offline comparison between
two consenting fleets -- e.g. two teams inside the same company comparing
notes -- see `commontrace overlap`, a separate, manual research tool that
exchanges signature files by hand and involves no Hub.)
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys

from commontrace import failure_import, hub_client, overlap, paths, trace_io
from commontrace.commands import _format
from commontrace.commands._format import read_or_warn

# Kept in step with hub/commons.py's signing. Both sides sign a trace on
# title + context + tags -- the *situation*, not the fix -- so the
# comparison is symmetric. A change here without the matching change there
# silently degrades every similarity score rather than failing.
COMMONS_NUM_PERM = overlap.DEFAULT_NUM_PERM


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "commons",
        help="Consult the CommonTrace Knowledge Base (operator-maintained, optional) -- "
        "not another fleet's data.",
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
    sign.add_argument(
        "--format", dest="from_format", choices=["jsonl", "json", "csv", "lines"], default=None,
        help="Force how --from is parsed instead of guessing from its extension/content. "
        "Use this if a file is misdetected (e.g. a .txt log whose lines start with '[').",
    )
    sign.add_argument("--dest", default=None)
    sign.set_defaults(func=run_sign)

    rep = sub.add_parser(
        "report",
        help="Ask the Knowledge Base how much of your failure set it already covers.",
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
    rep.add_argument(
        "--format", dest="from_format", choices=["jsonl", "json", "csv", "lines"], default=None,
        help="Force how --from is parsed instead of guessing from its extension/content.",
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

    ask = sub.add_parser(
        "ask",
        help="Ask the Knowledge Base what it already knows about one failure, in your "
        "own words. Returns ranked candidate answers -- the lookup, not the coverage %%.",
    )
    ask.add_argument(
        "question",
        help="The failure, described however you would describe it to a colleague. "
        "Signed locally: the text never leaves this machine.",
    )
    ask.add_argument(
        "--limit", type=int, default=None,
        help="How many candidates to return (default 5). Measured recall is 89.1%% at "
        "rank 1 and 100%% within the top 10.",
    )
    ask.add_argument(
        "--agent-type", default="",
        help="Narrow the corpus to one kind of agent (a support fleet's failures "
        "should not be scored against CUDA substrate).",
    )
    ask.add_argument("--json", action="store_true", help="Emit raw JSON instead of markdown.")
    ask.add_argument("--hub-url", default=None, help="Default: $COMMONTRACE_HUB_URL")
    ask.add_argument("--hub-api-key", default=None, help="Default: $COMMONTRACE_HUB_API_KEY")
    ask.set_defaults(func=run_ask)

    usage = sub.add_parser(
        "usage",
        help="What your plan entitles you to this period, and how much you have used.",
    )
    usage.add_argument("--hub-url", default=None)
    usage.add_argument("--hub-api-key", default=None)
    usage.set_defaults(func=run_usage)

    submit = sub.add_parser(
        "submit",
        help="Propose an entry for the Knowledge Base (operator-reviewed; publishes "
        "nothing by itself).",
    )
    submit.add_argument("--title", required=True, help="Short, symptom-first summary.")
    submit.add_argument(
        "--context", dest="context_text", required=True,
        help="When/how this happens -- the situation, written as substrate knowledge, "
        "not as your incident.",
    )
    submit.add_argument("--solution", dest="solution_text", required=True, help="The fix.")
    submit.add_argument("--tags", default="", help="Comma-separated tags, e.g. stripe,webhooks.")
    submit.add_argument("--agent-type", default="", help="Kind of agent this applies to.")
    submit.add_argument(
        "--rationale", required=True,
        help="Why this is substrate knowledge and not your business logic -- required, "
        "and it is the first thing an operator reads.",
    )
    submit.add_argument("--hub-url", default=None)
    submit.add_argument("--hub-api-key", default=None)
    submit.set_defaults(func=run_submit)

    submissions = sub.add_parser(
        "submissions",
        help="Check the status of your own Knowledge Base submissions.",
    )
    submissions.add_argument("--limit", type=int, default=None)
    submissions.add_argument("--json", action="store_true", help="Emit raw JSON instead of a table.")
    submissions.add_argument("--hub-url", default=None)
    submissions.add_argument("--hub-api-key", default=None)
    submissions.set_defaults(func=run_submissions)


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
        result = read_or_warn(trace_io.read, path)
        if result is None:
            continue
        instance, _ = result
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


def signatures_from_file(path: str, fmt_override: str | None = None) -> tuple[list[dict], dict]:
    """Sign failures a fleet already has, identically to build_signatures().

    Same `overlap.minhash` over the same title+text+tags concatenation, so a
    signature produced from an incident export is comparable to one produced
    from a `memory/traces/` store and to a Hub trace. If these ever diverge
    the numbers stay plausible and become meaningless, which is the worst
    failure mode available here -- hence one shared code path for the text.
    """
    failures, stats = failure_import.read_failures(path, fmt_override=fmt_override)
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


# Kept as a module-level name (not called inline as _format.resolve_hub)
# because tests monkeypatch commons_cmd._resolve_hub directly to stub out
# Hub connectivity -- every run_* function below resolves this name at
# call time, so the monkeypatch still takes effect regardless of where the
# real implementation lives. See commontrace/commands/_format.py:resolve_hub
# for the implementation, shared with account_cmd.py.
_resolve_hub = _format.resolve_hub


def run_sign(args: argparse.Namespace) -> int:
    stats = None
    if getattr(args, "from_file", None):
        try:
            failures, stats = signatures_from_file(args.from_file, fmt_override=getattr(args, "from_format", None))
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
        "# Knowledge Base Coverage Report",
        "",
        f"**{n_cov} of {n_f}** of your recurring failures "
        f"(**{frac:.0%}**) are already solved in the CommonTrace Knowledge Base.",
        "",
        f"- Knowledge Base entries searched: {report['n_commons_traces']}",
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
        lines += ["## What the Knowledge Base already knows", ""]
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
            failures, stats = signatures_from_file(args.from_file, fmt_override=getattr(args, "from_format", None))
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


def sign_question(question: str) -> list[int]:
    """Sign a free-text question the same way a stored failure is signed.

    Identical concatenation and identical `overlap.minhash` as
    build_signatures(), because a question and a trace must land in the same
    signature space or every similarity score is meaningless. A question has
    no separate title/context/tags, so the whole string plays all three
    roles -- which is exactly what build_signatures() does anyway once it
    joins them with spaces.
    """
    return overlap.minhash(question, COMMONS_NUM_PERM)


def _render_candidates(result: dict, question: str) -> str:
    candidates = result.get("candidates") or []
    lines = [f"# Knowledge Base: {question}", ""]

    if not candidates:
        total = result.get("n_commons_traces_total", 0)
        if not total:
            lines.append(
                "The Knowledge Base has no entries yet, so there is nothing to search. "
                "This is a cold start, not a finding."
            )
        else:
            lines.append(
                f"No entry shared a single content word with your question, across "
                f"{total:,} Knowledge Base entries. Matching is lexical, so try the "
                "wording an on-call engineer would use for the symptom."
            )
        return "\n".join(lines)

    lines.append(
        f"**{len(candidates)} candidate answer(s)** from {result.get('n_commons_traces', 0):,} "
        "shared traces. Ranked by similarity — judge them, do not assume them."
    )
    lines.append("")

    for c in candidates:
        trace = c.get("trace") or {}
        title = trace.get("title") or "(untitled)"
        lines.append(f"## {c.get('rank', '?')}. {title}")
        sim = c.get("similarity")
        bits = [f"similarity {sim:.3f}" if isinstance(sim, (int, float)) else "similarity ?"]
        hits = c.get("commons_hits")
        if hits:
            # The Stack-Overflow-shaped corroboration signal: this entry has
            # demonstrably covered a real recurring failure before, for this
            # fleet or another customer -- not a vote, an actual match.
            bits.append(f"has covered {hits:,} recurring failure(s) before")
        trust = trace.get("trust")
        if isinstance(trust, (int, float)) and trust:
            bits.append(f"trust {trust:.2f}")
        if trace.get("agent_type"):
            bits.append(str(trace["agent_type"]))
        lines.append("*" + " · ".join(bits) + "*")
        lines.append("")
        if trace.get("context_text"):
            lines.append(f"**When it happens:** {trace['context_text']}")
            lines.append("")
        if trace.get("solution_text"):
            lines.append(f"**Solution:** {trace['solution_text']}")
            lines.append("")
        if trace.get("tags"):
            lines.append("`" + "` `".join(str(t) for t in trace["tags"]) + "`")
            lines.append("")

    if result.get("corpus_truncated"):
        lines.append(
            f"*Searched the {result.get('n_commons_traces', 0):,} most recent of "
            f"{result.get('n_commons_traces_total', 0):,} Knowledge Base entries "
            "(per-query scan limit). Narrow with --agent-type for a tighter search.*"
        )
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*{result.get('note', '')}*")
    return "\n".join(lines)


def run_ask(args: argparse.Namespace) -> int:
    question = (args.question or "").strip()
    if not question:
        print("[commontrace] ask what? Pass the failure as a quoted argument.", file=sys.stderr)
        return 1

    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved

    if not args.json:
        print("[commontrace] signing your question locally -- the text does not leave "
              "this machine.\n")

    try:
        result = asyncio.run(
            hub_client.commons_search(
                hub_url, api_key, sign_question(question),
                limit=args.limit, agent_type=args.agent_type,
            )
        )
    except (hub_client.HubClientUnavailable, hub_client.HubConnectionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2) if args.json else _render_candidates(result, question))
    return 0


def run_usage(args: argparse.Namespace) -> int:
    """Show the meter: a flat plan allowance, plus whatever this org has
    permanently earned via accepted Knowledge Base submissions
    (`commons submit`) -- never by the act of submitting alone. See
    hub/plans.py "why bonus_commons_queries is not the same mistake
    twice"."""
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
    print(f"  traces stored:      {r['traces']['used']:,} of {fmt(r['traces']['limit'])}")
    print(f"  knowledge base queries: {q['used']:,} of {fmt(q['allowance'])}"
          f"   ({fmt(q['remaining'])} remaining)")
    bonus = q.get("bonus_from_accepted_submissions", 0)
    if bonus:
        print(f"    of which {bonus:,} earned via accepted `commons submit` proposals")
    return 0


def run_submit(args: argparse.Namespace) -> int:
    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved

    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]

    try:
        result = asyncio.run(
            hub_client.submit_kb_entry(
                hub_url, api_key,
                title=args.title, context_text=args.context_text, solution_text=args.solution_text,
                tags=tags, agent_type=args.agent_type, rationale=args.rationale,
            )
        )
    except (hub_client.HubClientUnavailable, hub_client.HubConnectionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print(f"[commontrace] submitted {result['id']} for review (status: {result['status']}).")
    print("  Nothing is published yet. An operator reviews it before anything changes;")
    print("  check status with `commontrace commons submissions`.")
    return 0


def run_submissions(args: argparse.Namespace) -> int:
    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved

    try:
        result = asyncio.run(hub_client.list_my_kb_submissions(hub_url, api_key, limit=args.limit))
    except (hub_client.HubClientUnavailable, hub_client.HubConnectionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    submissions = result.get("submissions") or []
    if not submissions:
        print("[commontrace] no submissions yet. `commontrace commons submit --help` to propose one.")
        return 0

    for s in submissions:
        print(f"{s['id']}  status={s['status']}  submitted={s['created_at']}")
        print(f"    {s['title']!r}")
        if s["status"] == "approved":
            print(f"    -> published; +{s['credit_awarded']} Knowledge Base queries credited")
        elif s["status"] == "rejected" and s.get("rejection_reason"):
            print(f"    reason: {s['rejection_reason']!r}")
    return 0
