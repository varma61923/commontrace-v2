from __future__ import annotations

import argparse
import datetime
import glob
import os
import sys

from commontrace import distill, frontmatter, lesson_io, paths, templates, trace_io
from commontrace.commands import _llm_draft
from commontrace.commands._validators import similarity_threshold as _similarity_threshold
from commontrace.frontmatter import FrontmatterError


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "distill",
        help="Curator step: find repeated patterns across memory/traces/ and propose "
        "candidate lessons at status=review (never auto-activated).",
    )
    p.add_argument("--agent-type", default=None, help="Only cluster traces of this agent_type.")
    p.add_argument("--similarity-threshold", type=_similarity_threshold, default=0.3)
    p.add_argument("--min-cluster-size", type=int, default=2)
    p.add_argument(
        "--draft", action="store_true",
        help="Ask a configured LLM (COMMONTRACE_LLM_API_KEY) to fill in the Rule, "
        "applies_when and do_not_apply_when from each cluster's own grouped evidence, "
        "instead of 'TODO: ...' placeholders. Falls back per-cluster, with a stated "
        "reason, if no provider is configured or it refuses.",
    )
    p.add_argument(
        "--failed", action="store_true",
        help="Only cluster traces recorded as failures (outcome.resolved false or repeated_error): "
        "a lesson drafted from what went wrong, not from everything that happened.",
    )
    p.add_argument(
        "--signal", default=None, metavar="NAME",
        help="Only the traces of one named failure signal (exact name from `commontrace signals list`); "
        "implies --failed.",
    )
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def _safe_tags(raw: object) -> list[str]:
    return [str(t) for t in raw if t is not None] if isinstance(raw, (list, tuple)) else []


def _iter_trace_paths(root: str):
    tdir = paths.traces_dir(root)
    for p in sorted(glob.glob(os.path.join(tdir, "*.md"))):
        if os.path.basename(p) == "README.md":
            continue
        yield p


def _iter_lesson_paths(root: str):
    ldir = paths.lessons_dir(root)
    for p in sorted(glob.glob(os.path.join(ldir, "lesson_*.md"))):
        if os.path.basename(p) == "lesson_template.md":
            continue
        yield p


def _load_traces(root: str, agent_type: str | None) -> list[distill.TraceCandidate]:
    out = []
    for path in _iter_trace_paths(root):
        try:
            instance, _ = trace_io.read(path)
        except FrontmatterError as exc:
            print(f"[commontrace] warning: skipping unreadable trace {path}: {exc}", file=sys.stderr)
            continue
        if agent_type and instance.get("agent_type") != agent_type:
            continue
        if not instance.get("id"):
            continue
        out.append(
            distill.TraceCandidate(
                id=instance["id"],
                path=path,
                title=instance.get("title", ""),
                context_text=instance.get("context_text", ""),
                solution_text=instance.get("solution_text", ""),
                tags=_safe_tags(instance.get("tags")),
                agent_type=instance.get("agent_type", ""),
            )
        )
    return out


def _failure_scope(root: str, args: argparse.Namespace) -> tuple[set[str], str | None]:
    from commontrace import failure_signals

    if not args.signal:
        return {o.id for o in failure_signals.load_failure_occurrences(root, args.agent_type)}, None
    signals, _ = failure_signals.build_signals(root, agent_type=args.agent_type)
    for signal in signals:
        if signal.name == args.signal:
            return set(signal.trace_ids), None
    names = ", ".join(repr(x.name) for x in signals[:5]) or "none found"
    return set(), f"no failure signal named {args.signal!r} (signals: {names}). See `commontrace signals list`."


def _existing_source_traces(root: str) -> list[list[str]]:
    out = []
    for path in _iter_lesson_paths(root):
        try:
            fm, _ = frontmatter.read(path)
        except FrontmatterError as exc:
            print(f"[commontrace] warning: skipping unreadable lesson {path}: {exc}", file=sys.stderr)
            continue
        out.append(list(fm.get("source_traces") or []) + list(fm.get("source_episodes") or []))
    return out


def _unique_candidate_slug(ldir: str, date: str, n: int) -> str:
    i = n
    while True:
        slug = f"lesson_candidate_{date}_{i}"
        if not os.path.isfile(os.path.join(ldir, f"{slug}.md")):
            return slug
        i += 1


def _evidence_lines(cluster: distill.Cluster) -> list[str]:
    n = len(cluster.traces)
    contexts = distill.variants([t.context_text for t in cluster.traces])
    solutions = distill.variants([t.solution_text for t in cluster.traces])
    lines = [f"{n} traces show this pattern.", "", "The situation:"]
    lines += _variant_lines(contexts, n)
    lines += ["", "What worked:"]
    lines += _variant_lines(solutions, n)
    return lines


def _candidate_body(cluster: distill.Cluster, llm_draft=None) -> list[str]:
    n = len(cluster.traces)
    contexts = distill.variants([t.context_text for t in cluster.traces])
    solutions = distill.variants([t.solution_text for t in cluster.traces])

    lines = [
        "## Rule",
        llm_draft.rule if llm_draft else "TODO: one actionable sentence, derived from `What worked` below.",
        "",
        "## Why",
        f"{n} traces show this pattern. Grouped, they say:",
        "",
        "**The situation**",
        "",
    ]
    lines += _variant_lines(contexts, n)
    lines += ["", "**What worked**", ""]
    lines += _variant_lines(solutions, n)
    if len(solutions) > 1:
        lines += [
            "",
            f"> Note: {len(solutions)} different resolutions for the same symptom. "
            "That usually means this is more than one problem — consider splitting "
            "the candidate, or narrowing `applies_when` until it covers only one.",
        ]
    lines += [
        "",
        "<!-- Source traces are listed in `source_traces` above. -->",
        "",
        "## How to apply",
        llm_draft.applies_when if llm_draft else "TODO: when to invoke it, how to use it concretely.",
        "",
        "## Counter-examples",
        llm_draft.do_not_apply_when if llm_draft else "TODO: cases where the rule does NOT apply.",
    ]
    if llm_draft is not None:
        lines += ["", "## LLM draft evidence", f"Cited: {', '.join(llm_draft.evidence) or '(none)'}"]
        if llm_draft.unverifiable_evidence:
            lines += [
                f"Cited but NOT among this cluster's traces (review before trusting): "
                f"{', '.join(llm_draft.unverifiable_evidence)}",
            ]
    return lines


def _variant_lines(items: list[tuple[str, int]], total: int) -> list[str]:
    if not items:
        return ["- _(none recorded)_"]
    shown = sum(count for _text, count in items)
    lines = [f"- ({count} of {total}) {text}" for text, count in items]
    if shown < total:
        lines.append(f"- _…and {total - shown} further variant(s), each seen once._")
    return lines


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    traces = _load_traces(root, args.agent_type)
    if args.failed or args.signal:
        keep, error = _failure_scope(root, args)
        if error:
            print(f"[commontrace] {error}", file=sys.stderr)
            return 2
        traces = [t for t in traces if t.id in keep]
        if not traces:
            print("[commontrace] no failed traces in scope -- nothing to distill.")
            return 0
    if not traces:
        print("[commontrace] no traces found under memory/traces/ -- nothing to distill.")
        return 0

    existing_source_traces = _existing_source_traces(root)
    clusters = distill.find_clusters(
        traces,
        existing_source_traces,
        similarity_threshold=args.similarity_threshold,
        min_cluster_size=args.min_cluster_size,
    )

    if not clusters:
        print(
            f"[commontrace] {len(traces)} trace(s) considered, no repeated pattern found "
            f"(>= {args.min_cluster_size} traces, similarity >= {args.similarity_threshold}). "
            "Nothing proposed."
        )
        if len(traces) < args.min_cluster_size:
            print(
                f"  Distilling finds rules by REPETITION, so it needs at least "
                f"{args.min_cluster_size} similar traces.\n"
                "  With fewer, write the rule directly -- one trace is enough when you "
                "already know the lesson:\n"
                "    commontrace lesson new --slug lesson_my_rule --description '...' "
                "--domain '...'"
            )
        else:
            print(
                "  The traces are there but none look alike enough to generalise from. "
                "Either keep capturing,\n"
                "  lower the bar with `--similarity-threshold` (default "
                f"{args.similarity_threshold}), or write the rule directly:\n"
                "    commontrace lesson new --slug lesson_my_rule --description '...' "
                "--domain '...'"
            )
        return 0

    ldir = paths.lessons_dir(root)
    paths.warn_if_implicit_cwd_store(args.dest)
    os.makedirs(ldir, exist_ok=True)
    date = datetime.date.today().strftime("%Y%m%d")

    print(f"[commontrace] {len(traces)} trace(s) considered, {len(clusters)} candidate cluster(s) found:\n")
    for n, cluster in enumerate(clusters, start=1):
        agent_type = cluster.traces[0].agent_type or (args.agent_type or paths.GENERAL_AGENT_TYPE)
        slug = _unique_candidate_slug(ldir, date, n)

        llm_draft = None
        if args.draft:
            llm_draft = _llm_draft.try_draft(
                instruction=(
                    "These traces show a repeated pattern. Propose ONE actionable rule "
                    "that generalizes what worked, precisely when it applies, and "
                    "when it does NOT apply."
                ),
                slug=slug,
                current_rule_text="(none yet -- this is a brand-new candidate, not a revision)",
                applies_when="(none yet)", do_not_apply_when="(none yet)",
                evidence_lines=_evidence_lines(cluster),
                allowed_evidence_ids={t.id for t in cluster.traces},
            )

        fm = templates.lesson_frontmatter(
            slug=slug,
            description=distill.propose_description(cluster),
            agent_type=agent_type,
            domain=distill.propose_domain(cluster, agent_type),
            tags=distill.propose_tags(cluster),
            applies_when=(
                llm_draft.applies_when if llm_draft
                else "TODO: precise activation condition (auto-proposed, needs human review)"
            ),
            do_not_apply_when=(
                llm_draft.do_not_apply_when if llm_draft
                else "TODO: explicit counter-condition (auto-proposed, needs human review)"
            ),
            importance=3,
            importance_rationale=(
                "Auto-proposed (LLM-assisted) from a repeated trace pattern; needs human calibration."
                if llm_draft else
                "Auto-proposed from a repeated trace pattern; needs human calibration."
            ),
            source_traces=[t.id for t in cluster.traces],
            status="review",
        )
        if llm_draft is not None:
            fm["llm_draft"] = dict(
                llm_draft.provenance,
                cited_evidence=llm_draft.evidence,
                unverifiable_evidence=llm_draft.unverifiable_evidence,
            )
        body_lines = _candidate_body(cluster, llm_draft=llm_draft)
        out_path = os.path.join(ldir, f"{slug}.md")
        lesson_io.write_lesson(
            out_path, fm, "\n".join(body_lines) + "\n", root=root,
            actor="distill", reason=(
                f"auto-proposed from {len(cluster.traces)} traces" + (" (LLM-assisted)" if llm_draft else "")
            ),
        )

        print(
            f"  [{n}] {slug} <- {len(cluster.traces)} traces, shared terms: "
            f"{', '.join(cluster.shared_terms[:5])}" + (" [LLM-assisted]" if llm_draft else "")
        )
        print(f"      wrote {out_path}")

    print(
        f"\n[commontrace] {len(clusters)} candidate lesson(s) written at status=review. "
        "Review each with `commontrace lesson list --status review`, then "
        "`commontrace lesson approve <slug>` or `commontrace lesson reject <slug> --reason ...`."
    )
    return 0
