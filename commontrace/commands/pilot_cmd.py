from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import sys

from commontrace import (
    distill,
    evidence_io,
    experiment,
    impact,
    integrity,
    paths,
    pilot,
    reliability,
    taxonomy,
    trace_io,
)
from commontrace.commands import experiment_cmd
from commontrace.commands._shellout import run_script
from commontrace.commands._validators import similarity_threshold as _similarity_threshold
from commontrace.frontmatter import FrontmatterError


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "pilot",
        help="Run the 30-day pilot report end to end: map the issues into a taxonomy, "
        "check reinforcement progress, measure what changed, and give a yes/no on "
        "whether CommonTrace is fixing the issues worth fixing.",
    )
    p.add_argument("--agent-type", default=None)
    p.add_argument("--similarity-threshold", type=_similarity_threshold, default=0.3)
    p.add_argument("--min-cluster-size", type=int, default=2)
    p.add_argument("--min-evidence", type=int, default=reliability.DEFAULT_MIN_EVIDENCE)
    p.add_argument("--precision-floor", type=float, default=reliability.DEFAULT_PRECISION_FLOOR)
    p.add_argument("--cost-per-1k-tokens", type=float, default=None)
    p.add_argument("--value-per-error-avoided", type=float, default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("--html", action="store_true", help="Write HTML to memory/benchmark_reports/")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def _load_pilot_metrics(root: str, agent_type: str | None) -> dict | None:
    extra = ["--json"]
    if agent_type:
        extra += ["--agent-type", agent_type]
    rc, out = run_script(
        root, "benchmark/pilot_metrics.py", extra,
        "pilot_metrics.py ships inside the commontrace package, so this usually "
        "means a damaged install -- try `pip install --force-reinstall commontrace`.",
        capture=True,
    )
    if rc != 0:
        return None
    try:
        data = json.loads(out)
        if isinstance(data, dict) and "error" in data:
            return None
        return data
    except json.JSONDecodeError:
        return None


def _load_traces(root: str) -> list[tuple[str, dict]]:
    tdir = paths.traces_dir(root)
    traces: list[tuple[str, dict]] = []
    for path in sorted(glob.glob(os.path.join(tdir, "*.md"))):
        if os.path.basename(path) == "README.md":
            continue
        try:
            instance, _ = trace_io.read(path)
        except FrontmatterError as exc:
            print(f"[commontrace] warning: skipping unreadable trace {path}: {exc}", file=sys.stderr)
            continue
        traces.append((path, instance))
    return traces


def run(args: argparse.Namespace) -> int:
    if args.json and args.html:
        print("[commontrace] --json and --html are mutually exclusive.", file=sys.stderr)
        return 2

    root = paths.resolve_root(args.dest)

    raw_traces = _load_traces(root)
    all_instances = [inst for _, inst in raw_traces]
    matched_traces = [
        (path, inst) for path, inst in raw_traces
        if not args.agent_type or inst.get("agent_type") == args.agent_type
    ]
    trace_instances = [inst for _, inst in matched_traces]
    trace_candidates = [
        distill.TraceCandidate(
            id=inst["id"],
            path=path,
            title=inst.get("title", ""),
            context_text=inst.get("context_text", ""),
            solution_text=inst.get("solution_text", ""),
            tags=(
                [str(t) for t in inst.get("tags", []) if t is not None]
                if isinstance(inst.get("tags"), (list, tuple))
                else []
            ),
            agent_type=inst.get("agent_type", ""),
        )
        for path, inst in matched_traces
        if inst.get("id")
    ]
    lessons = evidence_io.load_active_lessons(root)
    tax = taxonomy.build_taxonomy(
        trace_candidates, lessons,
        similarity_threshold=args.similarity_threshold,
        min_cluster_size=args.min_cluster_size,
    )

    evidence = evidence_io.load_evidence(root, traces=all_instances)
    impact_report = impact.compute_impact(
        evidence, trace_instances,
        cost_per_1k_tokens=args.cost_per_1k_tokens,
        value_per_error_avoided=args.value_per_error_avoided,
    )

    scores = reliability.score_lessons(
        evidence, min_evidence=args.min_evidence, precision_floor=args.precision_floor,
    ) if evidence else []
    harmful_lesson_slugs = [s.slug for s in scores if s.verdict == reliability.VERDICT_HARMFUL]

    all_rows, _rate, _corrupt = experiment_cmd._load(root)
    holdout_rows, _wanted_salt, _other = experiment_cmd.scope_to_current_salt(root, all_rows)
    obs = experiment_cmd._observations(holdout_rows)
    causal_report = integrity.audit(holdout_rows) if holdout_rows else None
    n_lines = causal_report.n_assignments if causal_report else 0
    causal_effects = (
        None if not holdout_rows
        else (experiment.analyze(obs, sequential=True) if (obs and causal_report.readable) else [])
    )

    pilot_json = _load_pilot_metrics(root, args.agent_type)
    if pilot_json:
        resolution_baseline = pilot_json["baseline"]["resolution_rate"]["value"]
        resolution_current = pilot_json["current"]["resolution_rate"]["value"]
        n_baseline_traces = pilot_json["baseline"]["n_traces"]
        n_current_traces = pilot_json["current"]["n_traces"]
    else:
        resolution_baseline = resolution_current = None
        n_baseline_traces = n_current_traces = 0

    resolution_delta = None
    if resolution_baseline is not None and resolution_current is not None:
        if resolution_baseline == 0:
            resolution_delta = 1.0 if resolution_current > 0 else 0.0
        else:
            resolution_delta = (resolution_current - resolution_baseline) / resolution_baseline

    report = pilot.PilotReport(
        taxonomy=tax,
        impact=impact_report,
        n_active_lessons=len(lessons),
        n_holdout_assignments=n_lines,
        causal_effects=causal_effects,
        resolution_baseline=resolution_baseline,
        resolution_current=resolution_current,
        resolution_delta=resolution_delta,
        n_baseline_traces=n_baseline_traces,
        n_current_traces=n_current_traces,
        harmful_lesson_slugs=harmful_lesson_slugs,
    )

    if args.json:
        import dataclasses

        print(json.dumps({
            "taxonomy": taxonomy.to_dict(report.taxonomy),
            "impact": impact.to_dict(report.impact),
            "n_active_lessons": report.n_active_lessons,
            "n_holdout_assignments": report.n_holdout_assignments,
            "causal_effects": (
                None if report.causal_effects is None
                else [dataclasses.asdict(e) for e in report.causal_effects]
            ),
            "resolution_baseline": report.resolution_baseline,
            "resolution_current": report.resolution_current,
            "resolution_delta": report.resolution_delta,
            "n_baseline_traces": report.n_baseline_traces,
            "n_current_traces": report.n_current_traces,
            "harmful_lesson_slugs": report.harmful_lesson_slugs,
            "result": dataclasses.asdict(report.result),
        }, indent=2))
        return 0

    if args.html:
        ts_display = datetime.datetime.now().isoformat(timespec="seconds")
        out_dir = os.path.join(paths.memory_dir(root), "benchmark_reports")
        os.makedirs(out_dir, exist_ok=True)
        ts_file = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = os.path.join(out_dir, f"pilot_report_{ts_file}.html")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(pilot.render_html(report, ts_display))
        print(f"HTML report written: {out_path}")
        return 0

    print(pilot.render_markdown(report))
    return 0
