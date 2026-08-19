from __future__ import annotations

import argparse
import datetime
import glob
import os

from commontrace import distill, frontmatter, paths, templates, trace_io


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "distill",
        help="Curator step: find repeated patterns across memory/traces/ and propose "
        "candidate lessons at status=review (never auto-activated).",
    )
    p.add_argument("--agent-type", default=None, help="Only cluster traces of this agent_type.")
    p.add_argument("--similarity-threshold", type=float, default=0.3)
    p.add_argument("--min-cluster-size", type=int, default=2)
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


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
        instance, _ = trace_io.read(path)
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
                tags=list(instance.get("tags") or []),
                agent_type=instance.get("agent_type", ""),
            )
        )
    return out


def _existing_source_traces(root: str) -> list[list[str]]:
    out = []
    for path in _iter_lesson_paths(root):
        fm, _ = frontmatter.read(path)
        out.append(list(fm.get("source_traces") or []) + list(fm.get("source_episodes") or []))
    return out


def _unique_candidate_slug(ldir: str, date: str, n: int) -> str:
    i = n
    while True:
        slug = f"lesson_candidate_{date}_{i}"
        if not os.path.isfile(os.path.join(ldir, f"{slug}.md")):
            return slug
        i += 1


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    traces = _load_traces(root, args.agent_type)
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
        return 0

    ldir = paths.lessons_dir(root)
    os.makedirs(ldir, exist_ok=True)
    date = datetime.date.today().strftime("%Y%m%d")

    print(f"[commontrace] {len(traces)} trace(s) considered, {len(clusters)} candidate cluster(s) found:\n")
    for n, cluster in enumerate(clusters, start=1):
        agent_type = cluster.traces[0].agent_type or (args.agent_type or "code")
        slug = _unique_candidate_slug(ldir, date, n)
        fm = templates.lesson_frontmatter(
            slug=slug,
            description=distill.propose_description(cluster),
            agent_type=agent_type,
            domain=distill.propose_domain(cluster, agent_type),
            tags=distill.propose_tags(cluster),
            applies_when="TODO: precise activation condition (auto-proposed, needs human review)",
            do_not_apply_when="TODO: explicit counter-condition (auto-proposed, needs human review)",
            importance=3,
            importance_rationale="Auto-proposed from a repeated trace pattern; needs human calibration.",
            source_traces=[t.id for t in cluster.traces],
            status="review",
        )
        body_lines = [
            "## Rule",
            "TODO: derive the actionable rule from the traces below.",
            "",
            "## Why",
        ]
        for t in cluster.traces:
            excerpt = (t.context_text or "").strip().replace("\n", " ")[:140]
            body_lines.append(f"- `{t.title}` ({t.id}): {excerpt}")
        body_lines += [
            "",
            "## How to apply",
            "TODO: when to invoke it, how to use it concretely.",
            "",
            "## Counter-examples",
            "TODO: cases where the rule does NOT apply.",
        ]
        out_path = os.path.join(ldir, f"{slug}.md")
        frontmatter.write(out_path, fm, "\n".join(body_lines) + "\n")

        print(f"  [{n}] {slug} <- {len(cluster.traces)} traces, shared terms: {', '.join(cluster.shared_terms[:5])}")
        print(f"      wrote {out_path}")

    print(
        f"\n[commontrace] {len(clusters)} candidate lesson(s) written at status=review. "
        "Review each with `commontrace lesson list --status review`, then "
        "`commontrace lesson approve <slug>` or `commontrace lesson reject <slug> --reason ...`."
    )
    return 0
