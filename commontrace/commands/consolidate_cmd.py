from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys

from commontrace import (
    consolidate,
    evidence_io,
    frontmatter,
    lesson_io,
    paths,
    redundancy,
    reliability,
    templates,
)
from commontrace.commands import _llm_draft
from commontrace.commands._format import read_or_warn


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "consolidate",
        help="Propose fusions, archive candidates, and contradictions in the active "
        "corpus. Reports only -- never modifies a lesson. `consolidate facts` clusters "
        "near-duplicate atomic facts.",
    )
    p.add_argument(
        "target", nargs="?", choices=("lessons", "facts"), default="lessons",
        help="lessons (default): fusion/contradiction report; facts: near-duplicate fact clusters.",
    )
    facts = p.add_argument_group("facts", "options for `consolidate facts`")
    facts.add_argument("--threshold", type=float, default=None,
                       help="Stemmed token-set Jaccard at or above which two facts are linked "
                            "(default 0.5).")
    facts.add_argument("--embedder", default=None,
                       help="Also link facts whose embedding cosine reaches --embed-threshold; any "
                            "commontrace.embeddings tag (default: $COMMONTRACE_CONSOLIDATE_EMBEDDER, else off).")
    facts.add_argument("--embed-threshold", type=float, default=None, help="Cosine threshold (default 0.9).")
    facts.add_argument("--scope", default="", help="Only facts visible in this scope.")
    facts.add_argument("--summarize", choices=("extractive", "model", "none"), default="extractive",
                       help="Cluster summary: extractive (default), model (the configured LLM) or none.")
    facts.add_argument("--apply", action="store_true",
                       help="Write one status=review proposal per cluster. Facts are never changed or deleted.")
    p.add_argument(
        "--redundancy-threshold", type=float, default=redundancy.DEFAULT_THRESHOLD,
        help="Similarity (commontrace/redundancy.py) at or above which two active "
             "lessons are proposed as a fusion candidate. Same default this store's "
             "injection budget uses (`commontrace retrieval --redundancy-threshold`).",
    )
    p.add_argument(
        "--activation-overlap", type=float, default=reliability.DEFAULT_ACTIVATION_OVERLAP,
        help="Same flag `commontrace reliability` exposes -- how similar two "
             "activation conditions must be before a contradiction is possible.",
    )
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero if any fusion, archive, or high-severity contradiction "
        "candidate exists. For a periodic corpus-hygiene check in CI.",
    )
    p.add_argument(
        "--draft", action="store_true",
        help="Ask a configured LLM (COMMONTRACE_LLM_API_KEY) to draft a merged lesson "
        "for each fusion pair and a split-by-condition lesson for each high-severity "
        "contradiction, written at status=review. The originals are never changed.",
    )
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


_MERGE = (
    "These two active lessons say substantially the same thing. Propose ONE lesson "
    "that replaces both: a single rule, and the activation condition covering the "
    "cases either one covered."
)
_SPLIT = (
    "These two active lessons fire on overlapping situations but pull in opposite "
    "directions. Propose ONE lesson that resolves them: state in the rule which "
    "advice applies when, and make applies_when/do_not_apply_when separate the cases."
)


def _draft_from_pair(root: str, by_slug: dict, a: str, b: str, kind: str) -> str | None:
    slug = f"{lesson_io.canonical_slug(a)}-{kind}-{lesson_io.canonical_slug(b)}"[:120]
    out_path = os.path.join(paths.lessons_dir(root), f"lesson_{slug}.md")
    if os.path.exists(out_path):
        existing = read_or_warn(frontmatter.read, out_path)
        if existing is not None and existing[0].get("status") == "review":
            print(f"[commontrace] {slug}: a draft is already pending review -- skipped.", file=sys.stderr)
            return None

    lessons = [by_slug[a], by_slug[b]]
    evidence_lines = []
    for fm in lessons:
        evidence_lines += [
            f"Lesson {fm.get('name')}:",
            f"  applies_when: {fm.get('applies_when', '')}",
            f"  do_not_apply_when: {fm.get('do_not_apply_when', '')}",
            "  body:",
            *("    " + line for line in str(fm.get(templates.BODY_KEY, "")).splitlines()),
        ]
    draft = _llm_draft.try_draft(
        instruction=_MERGE if kind == "merge" else _SPLIT,
        slug=slug, current_rule_text="(see the two lessons below)",
        applies_when="(see the two lessons below)", do_not_apply_when="(see the two lessons below)",
        evidence_lines=evidence_lines, allowed_evidence_ids={a, b},
    )
    if draft is None:
        return None

    first = lessons[0]
    fm = templates.lesson_frontmatter(
        slug=slug,
        description=f"{'Merge' if kind == 'merge' else 'Split'} draft of {a} and {b}",
        agent_type=str(first.get("agent_type") or paths.store_agent_type(root)),
        domain=str(first.get("domain") or ""),
        tags=sorted({str(t) for fm_ in lessons for t in fm_.get("tags") or []}),
        applies_when=draft.applies_when,
        do_not_apply_when=draft.do_not_apply_when,
        importance=max(int(fm_.get("importance") or 3) for fm_ in lessons),
        importance_rationale=f"Drafted from {a} and {b}; calibrate before approving.",
        status="review",
        scopes=sorted({str(scope) for fm_ in lessons for scope in fm_.get("scopes") or []}),
    )
    fm["merges" if kind == "merge" else "reconciles"] = [a, b]
    fm["llm_draft"] = dict(
        draft.provenance, cited_evidence=draft.evidence, unverifiable_evidence=draft.unverifiable_evidence,
    )
    body = (
        f"## Rule\n{draft.rule}\n\n"
        f"## Why\nProposed {'merge' if kind == 'merge' else 'split by condition'} of `{a}` and `{b}`.\n\n"
        "## How to apply\nTODO: when to invoke it, how to use it concretely.\n\n"
        "## Counter-examples\nTODO: cases where the rule does NOT apply.\n"
    )
    lesson_io.write_lesson(
        out_path, fm, body, root=root, actor="consolidate",
        reason=f"consolidate --draft {kind} of {a} and {b}",
    )
    return slug


def _write_drafts(root: str, lessons: list[dict], report) -> list[str]:
    by_slug = {str(fm.get("name")): fm for fm in lessons}
    written = []
    jobs = [(p.a, p.b, "merge") for p in report.fuse]
    jobs += [(c.slug_a, c.slug_b, "split") for c in report.high_severity_contradictions]
    for a, b, kind in jobs:
        if a not in by_slug or b not in by_slug:
            continue
        slug = _draft_from_pair(root, by_slug, a, b, kind)
        if slug:
            written.append(slug)
    return written


def run_facts(args: argparse.Namespace) -> int:
    from commontrace import fact_consolidation as fc

    root = paths.resolve_root(args.dest)
    try:
        report = fc.cluster_facts(
            root,
            threshold=fc.DEFAULT_THRESHOLD if args.threshold is None else args.threshold,
            embedder=args.embedder,
            embed_threshold=fc.DEFAULT_EMBED_THRESHOLD if args.embed_threshold is None else args.embed_threshold,
            scope=args.scope, summarize=args.summarize,
        )
        if args.apply:
            report["applied"] = fc.apply_clusters(root, report)
    except (ValueError, RuntimeError, OSError, PermissionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.json else fc.render(report))
    if args.strict and report["clusters"]:
        print(f"\n[commontrace] --strict: {len(report['clusters'])} fact cluster(s).", file=sys.stderr)
        return 1
    return 0


def run(args: argparse.Namespace) -> int:
    if getattr(args, "target", "lessons") == "facts":
        return run_facts(args)
    root = paths.resolve_root(args.dest)
    lessons = evidence_io.load_active_lessons(root)

    if not lessons:
        print(
            "[commontrace] no active lessons to consolidate.\n"
            "  `commontrace lesson list --status active` shows what this store has.",
        )
        return 0

    report = consolidate.build_report(
        lessons,
        redundancy_threshold=args.redundancy_threshold,
        activation_overlap=args.activation_overlap,
    )

    if args.json:
        print(json.dumps({
            "n_active": report.n_active,
            "fuse": [dataclasses.asdict(p) for p in report.fuse],
            "contradict": [dataclasses.asdict(c) for c in report.contradict],
            "archive": list(report.archive),
        }, indent=2))
    else:
        print(consolidate.render(report))

    if args.draft:
        written = _write_drafts(root, lessons, report)
        print(
            f"[commontrace] --draft: {len(written)} draft(s) written at status=review"
            + (": " + ", ".join(written) if written else "."),
            file=sys.stderr,
        )

    if args.strict and not report.is_clean:
        high = report.high_severity_contradictions
        print(
            f"\n[commontrace] --strict: {len(report.fuse)} fusion candidate(s), "
            f"{len(high)} high-severity contradiction(s), "
            f"{len(report.archive)} never-retrieved lesson(s).",
            file=sys.stderr,
        )
        return 1
    return 0
