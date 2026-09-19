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

from commontrace import failure_import, hub_client, overlap, paths, semantic, trace_io
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
        "--corpus", default=None, metavar="FILE",
        help="Measure against a Knowledge Base corpus file ON THIS MACHINE instead "
        "of asking a Hub. Nothing is sent anywhere -- not your failure text, not a "
        "signature, not even the fact that you asked. The corpus is operator-curated "
        "public content, so this is a coverage number you can compute without "
        "trusting anyone with your incidents.",
    )
    rep.add_argument(
        "--semantic", action="store_true",
        help="With --corpus, match by sentence-embedding similarity instead of word "
        "overlap. Measured on the held-out probe set: 32.6%% recall at a 0%% "
        "false-positive bar, against the lexical matcher's 8.7%% "
        "(commons/eval/semantic.py). Needs the optional model stack: "
        "pip install 'commontrace[attention]'.",
    )
    rep.add_argument(
        "--counts-only", action="store_true",
        help="Ask for the headline number without pulling any matched content.",
    )
    rep.add_argument(
        "--candidates", action="store_true",
        help="For each failure the coverage bar did NOT clear, also look it up by "
        "ranked search and show the best candidate answers. Measured recall is 89%% "
        "at rank 1 against the coverage bar's 10.9%%, so this is usually the "
        "difference between a report that reads 'we know nothing about you' and one "
        "that is useful. Results are candidates to judge, never counted as coverage. "
        "Costs one consultation per uncovered failure looked up.",
    )
    rep.add_argument(
        "--candidate-limit", type=int, default=DEFAULT_CANDIDATE_LOOKUPS, metavar="N",
        help=f"With --candidates, look up at most N uncovered failures "
        f"(default {DEFAULT_CANDIDATE_LOOKUPS}). Each one costs a consultation, so "
        "this bounds what a single report can spend.",
    )
    rep.add_argument("--json", action="store_true", help="Emit raw JSON instead of markdown.")
    rep.add_argument("--hub-url", default=None, help="Default: $COMMONTRACE_HUB_URL")
    rep.add_argument("--hub-api-key", default=None, help="Default: $COMMONTRACE_HUB_API_KEY")
    rep.add_argument("--dest", default=None)
    rep.set_defaults(func=run_report)

    fetch = sub.add_parser(
        "fetch",
        help="Download the operator's Knowledge Base corpus so you can match "
        "against it locally, with nothing sent back.",
    )
    fetch.add_argument("--out", required=True, help="Where to write the corpus JSONL.")
    fetch.add_argument("--limit", type=int, default=None)
    fetch.add_argument("--hub-url", default=None, help="Default: $COMMONTRACE_HUB_URL")
    fetch.add_argument("--hub-api-key", default=None, help="Default: $COMMONTRACE_HUB_API_KEY")
    fetch.set_defaults(func=run_fetch)

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


# How many uncovered failures one `commons report --candidates` will look
# up unless told otherwise. Each lookup is a metered consultation
# (hub/crud.py:commons_search), so an unbounded report over a large import
# could spend a month's allowance in one command. Ten is enough to make the
# point on a realistic incident export while keeping the bill legible.
DEFAULT_CANDIDATE_LOOKUPS = 10


def _uncovered(failures: list[dict], report: dict) -> list[dict]:
    """The failures the coverage bar did not clear, in input order.

    Disputed matches count as *found*, not uncovered: the Knowledge Base
    demonstrably has something about them, and `_render` already gives them
    their own section explaining why they are excluded from the figure.
    Looking them up again would spend a consultation to repeat what the
    report just said.
    """
    answered = {m.get("failure_label") for m in (report.get("matches") or [])}
    answered |= {m.get("failure_label") for m in (report.get("disputed_matches") or [])}
    return [f for f in failures if f.get("label") not in answered]


def _render_report_candidates(found: list[dict], skipped: int) -> str:
    """The ranked lookups, under a heading that cannot be misread as coverage.

    Kept textually separate from the coverage section above, and never
    folded into its arithmetic, because commons/eval/RESULTS.md measured
    exactly why: the score distributions of true and absent matches
    overlap, so a ranked hit is evidence for a human to weigh and not a
    solved failure. The coverage figure keeps its 0% false-positive
    property precisely by not counting anything on this list.
    """
    lines = [
        "## Candidates to judge — NOT counted as coverage",
        "",
        "The coverage figure above uses a deliberately strict bar so that a number "
        "you may quote never over-claims. Measured against labelled pairs, that bar "
        "discards roughly nine of every ten real answers "
        "(`commons/eval/RESULTS.md`). Ranked lookup keeps the same privacy "
        "properties — still signatures, still no failure text leaving this machine — "
        "and finds the right entry 89% of the time at rank 1.",
        "",
        "So the entries below are what the Knowledge Base offers for failures the "
        "bar did not clear. They are **candidates for you to judge**, and none of "
        "them moved the percentage above.",
        "",
    ]
    for item in found:
        label = item["label"]
        candidates = item.get("candidates") or []
        lines += [f"### {label}", ""]
        if item.get("error"):
            lines += [f"> Lookup failed: {item['error']}", ""]
            continue
        if not candidates:
            lines += ["No entry shared a content word with this failure.", ""]
            continue
        for c in candidates:
            trace = c.get("trace") or {}
            sim = c.get("similarity")
            bits = [f"similarity {sim:.3f}"] if isinstance(sim, (int, float)) else []
            if trace.get("standing"):
                bits.append(str(trace["standing"]))
            votes = trace.get("vote_count") or 0
            if votes:
                bits.append(f"{votes} fleet(s) voted")
            lines += [
                f"- **{trace.get('title', '(untitled)')}**"
                + (f" — *{' · '.join(bits)}*" if bits else ""),
            ]
            solution = (trace.get("solution_text") or "").strip()
            if solution:
                lines.append(f"  - {solution}")
        lines.append("")
    if skipped > 0:
        lines += [
            f"*{skipped} further uncovered failure(s) were not looked up — each lookup "
            "spends a consultation. Raise `--candidate-limit` to include them.*",
            "",
        ]
    return "\n".join(lines)


def _render_candidates_offer(n_uncovered: int) -> str:
    """Shown when the bar cleared nothing (or little) and the reader did not
    ask for lookups.

    This exists because of a specific, recorded failure: a prospect's export
    of nine failures, seven of which this corpus provably contained, came
    back "0 of 9, 0%" — and a reader has no way to tell that from "this
    product knows nothing about my problems". The number was correct. The
    impression it left was false, and that gap is what loses the room.
    """
    return "\n".join([
        "## Before you read the number above as 'it knows nothing'",
        "",
        f"{n_uncovered} of your failures did not clear the coverage bar. That bar is "
        "strict on purpose — it buys a 0% false-positive rate so the percentage is "
        "safe to quote — and the price, measured against labelled pairs, is that it "
        "discards roughly nine of every ten answers the Knowledge Base actually has "
        "(`commons/eval/RESULTS.md`).",
        "",
        "**A low number here is not the same as an empty Knowledge Base.** Ranked "
        "lookup, with identical privacy properties, finds the right entry 89% of the "
        "time at rank 1. To see what it has for the failures above:",
        "",
        "```",
        "commontrace commons report --from <your file> --candidates",
        "```",
        "",
        f"That costs one consultation per failure looked up (at most "
        f"{DEFAULT_CANDIDATE_LOOKUPS} unless you raise `--candidate-limit`), and what "
        "comes back are candidates to judge rather than coverage.",
        "",
    ])


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

    # Matched, then deliberately left out of the count above. Shown rather
    # than dropped so the coverage figure never moves without the reader
    # being able to see why: a number that fell because the field found an
    # answer wrong is a different event from one that fell because the
    # corpus shrank, and a report that hides the difference invites the
    # wrong conclusion about both.
    disputed = report.get("disputed_matches") or []
    if disputed:
        lines += [
            "## Matched, but disputed — not counted above",
            "",
            f"{len(disputed)} of your failures matched a Knowledge Base entry that a "
            "majority of the fleets who tried it reported did not work. Those entries "
            "are excluded from the coverage figure, deliberately: a wrong answer is not "
            "a solved failure. They are listed here because the Knowledge Base having "
            "something contested about your failure is a different situation from it "
            "having nothing at all.",
            "",
        ]
        for m in disputed:
            trace = m.get("trace") or {}
            lines += [
                f"### {trace.get('title', '(untitled)')}",
                "",
                f"- Matches your `{m['failure_label']}` at similarity **{m['similarity']}**",
                f"- Reported as not working by a majority of "
                f"{trace.get('vote_count', 0)} fleet(s)",
                "",
                f"**Proposed solution (verify before applying):** "
                f"{trace.get('solution_text', '')}",
                "",
            ]
    return "\n".join(lines)


def _load_corpus(path: str) -> list[dict]:
    """A Knowledge Base corpus file, as `hub.manage commons-seed` consumes it."""
    records = []
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{path}:{n}: not valid JSON ({exc})") from None
            if not isinstance(rec, dict) or not rec.get("title"):
                raise ValueError(f"{path}:{n}: not a corpus record (needs a title)")
            records.append(rec)
    if not records:
        raise ValueError(f"{path}: no records")
    return records


def run_local_report(args: argparse.Namespace) -> int:
    """Coverage measured entirely on this machine, against a corpus file.

    No Hub, no network, no signature: the corpus is public operator-curated
    content and the failure text is already here, so both halves of the
    comparison are local. That makes this strictly less disclosing than the
    shipped path, which transmits a MinHash signature -- here the operator
    does not learn that you asked, let alone what about.

    The lexical matcher is the same `overlap` code the Hub runs, so
    `--corpus` alone reproduces the Hub's number offline. `--semantic`
    swaps in embeddings, whose measured recall is ~3.7x higher at the same
    zero-false-positive bar (commons/eval/semantic.py).
    """
    try:
        corpus = _load_corpus(args.corpus)
    except (OSError, ValueError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    if not getattr(args, "from_file", None):
        print("[commontrace] --corpus needs --from: a local run has no Hub to ask "
              "about this store, so point it at the failures to measure.",
              file=sys.stderr)
        return 1
    try:
        # The same reader signatures_from_file() uses, so the text both
        # matchers see is identical -- the two sides of this comparison
        # diverging is the failure mode that keeps numbers plausible while
        # making them meaningless.
        raw, stats = failure_import.read_failures(
            args.from_file, fmt_override=getattr(args, "from_format", None))
    except failure_import.FailureImportError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    labels = [f["label"] for f in raw]
    texts = [" ".join([f["label"], f["text"], " ".join(f["tags"])]) for f in raw]

    corpus_text = [
        " ".join([
            str(r.get("title") or ""), str(r.get("context_text") or ""),
            " ".join(_safe_tags(r.get("tags"))),
        ])
        for r in corpus
    ]

    if args.semantic:
        try:
            threshold = (args.threshold if args.threshold is not None
                         else semantic.DEFAULT_SEMANTIC_THRESHOLD)
            results = semantic.best_matches(texts, corpus_text, threshold=threshold)
        except semantic.SemanticUnavailable as exc:
            print(f"[commontrace] {exc}", file=sys.stderr)
            return 1
        matcher = f"semantic (cosine >= {threshold}, {semantic.MODEL_NAME})"
    else:
        threshold = (args.threshold if args.threshold is not None
                     else overlap.DEFAULT_MATCH_THRESHOLD)
        corpus_sigs = [overlap.minhash(t, COMMONS_NUM_PERM) for t in corpus_text]
        results = []
        for text in texts:
            sig = overlap.minhash(text, COMMONS_NUM_PERM)
            scored = sorted(
                ((i, overlap.estimate_jaccard(sig, cs))
                 for i, cs in enumerate(corpus_sigs)),
                key=lambda pair: -pair[1],
            )
            results.append({
                "best": scored[0] if scored else None,
                "matches": [(i, sc) for i, sc in scored[:5] if sc >= threshold],
            })
        matcher = f"lexical (jaccard >= {threshold})"

    covered = [r for r in results if r["matches"]]
    report = {
        "n_failures": len(results),
        "n_covered": len(covered),
        "covered_fraction": (len(covered) / len(results)) if results else 0.0,
        "n_commons_traces": len(corpus),
        "matcher": matcher,
        "local": True,
        "entries": [
            {
                "failure_label": labels[i],
                "matches": [
                    {"title": corpus[j].get("title"), "similarity": round(sc, 3),
                     "solution_text": corpus[j].get("solution_text", "")}
                    for j, sc in r["matches"]
                ],
                "best_unmatched": (
                    {"title": corpus[r["best"][0]].get("title"),
                     "similarity": round(r["best"][1], 3)}
                    if r["best"] and not r["matches"] else None
                ),
            }
            for i, r in enumerate(results)
        ],
    }

    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(_render_local(report, stats))
    return 0


def _render_local(report: dict, stats: dict) -> str:
    n, c = report["n_failures"], report["n_covered"]
    lines = [
        "# Knowledge Base Coverage Report (computed locally)",
        "",
        f"**{c} of {n}** of your failures (**{report['covered_fraction']:.0%}**) match "
        f"an entry in this Knowledge Base corpus.",
        "",
        f"- Corpus entries searched: {report['n_commons_traces']}",
        f"- Matcher: {report['matcher']}",
        "",
        "> Computed entirely on this machine. No failure text, no signature and no "
        "record that you ran this left it — there was no Hub in this measurement.",
        "",
    ]
    covered = [e for e in report["entries"] if e["matches"]]
    if covered:
        lines += ["## What this corpus already knows", ""]
        for e in covered:
            top = e["matches"][0]
            lines += [
                f"### {top['title']}",
                "",
                f"- Matches your `{e['failure_label']}` at **{top['similarity']}**",
                "",
                f"**Solution:** {top['solution_text']}",
                "",
            ]
    near = [e for e in report["entries"] if e["best_unmatched"]]
    if near:
        lines += [
            "## Nearest entry for the rest — below the bar, shown anyway",
            "",
            "A matcher that hides its own runner-up makes the threshold's cost "
            "invisible. None of these counted toward the figure above.",
            "",
        ]
        for e in near:
            b = e["best_unmatched"]
            lines.append(f"- `{e['failure_label']}` → *{b['title']}* ({b['similarity']})")
        lines.append("")
    return "\n".join(lines)


def run_report(args: argparse.Namespace) -> int:
    # --corpus means "measure here", so it is answered before any Hub is
    # resolved: a local run must not require a hub url or an api key, and
    # must not be able to reach the network by accident.
    if getattr(args, "corpus", None):
        return run_local_report(args)
    if getattr(args, "semantic", False):
        print("[commontrace] --semantic needs --corpus: the Hub serves MinHash "
              "signatures, not embeddings, so semantic matching runs locally "
              "against a corpus file.", file=sys.stderr)
        return 1
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

    # The coverage call, its threshold and its output are untouched by
    # everything below: the ranked lookups are a second, separate question
    # asked only about the failures that call did not answer.
    uncovered = _uncovered(failures, report)
    looked_up: list[dict] = []
    skipped = 0
    if uncovered and getattr(args, "candidates", False):
        try:
            budget = int(getattr(args, "candidate_limit", DEFAULT_CANDIDATE_LOOKUPS))
        except (TypeError, ValueError):
            budget = DEFAULT_CANDIDATE_LOOKUPS
        budget = max(0, budget)
        skipped = max(0, len(uncovered) - budget)
        for failure in uncovered[:budget]:
            entry: dict = {"label": failure.get("label", "(unlabelled)")}
            try:
                found = asyncio.run(
                    hub_client.commons_search(
                        hub_url, api_key, failure.get("signature") or [],
                    )
                )
                entry["candidates"] = found.get("candidates") or []
            except (hub_client.HubClientUnavailable, hub_client.HubConnectionError) as exc:
                # One failed lookup must not discard the coverage report the
                # caller already paid for, nor the lookups that did succeed
                # -- a plan running out of consultations mid-report is the
                # expected case, not an exceptional one.
                entry["error"] = str(exc)
            looked_up.append(entry)

    if args.json:
        if looked_up or skipped:
            report = dict(report)
            report["candidate_lookups"] = looked_up
            report["candidate_lookups_skipped"] = skipped
            # Named so no consumer can mistake this for part of the
            # coverage arithmetic, which is what RESULTS.md warns against.
            report["candidate_lookups_are_not_coverage"] = True
        print(json.dumps(report, indent=2))
        return 0

    rendered = _render(report)
    if looked_up:
        rendered += "\n" + _render_report_candidates(looked_up, skipped)
    elif uncovered:
        rendered += "\n" + _render_candidates_offer(len(uncovered))
    print(rendered)
    return 0


def run_fetch(args: argparse.Namespace) -> int:
    """Pull the curated corpus down so every later question can be answered
    here, without asking.

    This is the one Knowledge Base call that carries no signature and names
    no failure: it asks for public curated content, so the Hub learns only
    that a fetch happened -- not what this fleet is failing at. Afterwards
    `commons report --corpus` needs no network at all, which is why this
    exists rather than a flag that fetches implicitly on every report.
    """
    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved

    try:
        result = asyncio.run(hub_client.commons_export(hub_url, api_key, limit=args.limit))
    except (hub_client.HubClientUnavailable, hub_client.HubConnectionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    entries = result.get("entries") or []
    if not entries:
        print("[commontrace] the Knowledge Base returned no entries.", file=sys.stderr)
        return 1

    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[commontrace] cannot write {args.out}: {exc}", file=sys.stderr)
        return 1

    print(f"[commontrace] wrote {len(entries)} entry(ies) to {args.out}")
    if result.get("truncated"):
        print("[commontrace] the corpus was truncated at the server's limit; "
              "pass --limit to ask for more.", file=sys.stderr)
    print("[commontrace] from here nothing needs to leave this machine:")
    print(f"    commontrace commons report --from <your failures> --corpus {args.out}")
    print("    ... add --semantic for the higher-recall matcher "
          "(pip install 'commontrace[attention]').")
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
        votes = trace.get("vote_count") or 0
        if isinstance(trust, (int, float)) and votes:
            # Trust without its denominator is not readable: 0.00 from one
            # downvote and 0.00 from twelve are the same number and
            # completely different facts. Suppressed entirely at zero votes,
            # where 0.5 is a placeholder rather than a measurement.
            bits.append(f"trust {trust:.2f} from {votes} fleet(s)")
        if trace.get("agent_type"):
            bits.append(str(trace["agent_type"]))
        lines.append("*" + " · ".join(bits) + "*")
        lines.append("")
        if trace.get("standing") == "disputed":
            lines.append(
                "> **Disputed.** A majority of the fleets that tried this entry reported "
                "it did not work. It is ranked last and shown anyway, because a contested "
                "answer is still more than no answer — but verify it before applying it."
            )
            lines.append("")
        elif trace.get("standing") == "stale":
            lines.append(
                "> **Past its review date.** Nobody has said this is wrong; nobody has "
                "confirmed it is still right either. Version-pinned advice ages."
            )
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
