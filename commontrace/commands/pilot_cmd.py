from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

from commontrace import evidence_io, experiment, impact, paths, pilot, reliability, taxonomy
from commontrace.commands import experiment_cmd
from commontrace.commands._shellout import run_script
from commontrace.commands._traces import load_trace_candidates, load_trace_instances


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "pilot",
        help="Run the 30-day pilot report end to end: map the issues into a taxonomy, "
        "check reinforcement progress, measure what changed, and give a yes/no on "
        "whether CommonTrace is fixing the issues worth fixing. See PILOT.md.",
    )
    p.add_argument("--agent-type", default=None)
    p.add_argument("--similarity-threshold", type=float, default=0.3)
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
    """Baseline-vs-current resolution rate, via the same script `commontrace
    bench --pilot` runs -- one number, computed one way, everywhere it's used."""
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
        return json.loads(out)
    except json.JSONDecodeError:
        # pilot_metrics.py prints a plain-text "no traces" notice (not JSON)
        # and exits 0 when the store has no outcome data at all -- that is
        # "no baseline yet", not a failure of this command.
        return None


def run(args: argparse.Namespace) -> int:
    if args.json and args.html:
        print("[commontrace] --json and --html are mutually exclusive.", file=sys.stderr)
        return 2

    root = paths.resolve_root(args.dest)

    trace_candidates = load_trace_candidates(root, args.agent_type)
    lessons = evidence_io.load_active_lessons(root)
    tax = taxonomy.build_taxonomy(
        trace_candidates, lessons,
        similarity_threshold=args.similarity_threshold,
        min_cluster_size=args.min_cluster_size,
    )

    evidence = evidence_io.load_evidence(root)
    trace_instances = load_trace_instances(root, args.agent_type)
    impact_report = impact.compute_impact(
        evidence, trace_instances,
        cost_per_1k_tokens=args.cost_per_1k_tokens,
        value_per_error_avoided=args.value_per_error_avoided,
    )

    scores = reliability.score_lessons(
        evidence, min_evidence=args.min_evidence, precision_floor=args.precision_floor,
    ) if evidence else []
    harmful_lesson_slugs = [s.slug for s in scores if s.verdict == reliability.VERDICT_HARMFUL]

    obs, _rate, n_lines, _n_no_outcome, _n_dup, _n_corrupt = experiment_cmd._load_observations(root)
    causal_effects = None if n_lines == 0 else (experiment.analyze(obs) if obs else [])

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
    if resolution_baseline not in (None, 0) and resolution_current is not None:
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
