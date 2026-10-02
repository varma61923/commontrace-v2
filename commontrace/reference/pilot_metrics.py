#!/usr/bin/env python3
"""pilot_metrics.py — CommonTrace's five business-outcome metrics."""
import argparse
import datetime
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import measure_performance as mp

SCHEMA_VERSION = "1.0.0"

_ROOT = (
    os.environ.get("COMMONTRACE_ROOT")
    or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
TRACES_DIR = os.path.join(_ROOT, "memory", "traces")

_SECTION_RE = re.compile(r"^##\s*(Context|Solution)\s*\n(.*?)(?=\n##\s|\Z)", re.DOTALL | re.MULTILINE)


def load_traces(root=None, agent_type=None):
    tdir = os.path.join(root, "memory", "traces") if root else TRACES_DIR
    traces = []
    for p in sorted(glob.glob(os.path.join(tdir, "*.md"))):
        if os.path.basename(p) == "README.md":
            continue
        try:
            with open(p, encoding="utf-8-sig") as fh:
                fm = mp.parse_frontmatter(fh.read())
        except OSError:
            continue
        if not fm:
            continue
        if agent_type and fm.get("agent_type") != agent_type:
            continue
        fm["_path"] = p
        traces.append(fm)
    return traces


def _rate(traces, field):
    values = [t.get("outcome", {}).get(field) for t in traces if isinstance(t.get("outcome"), dict)]
    values = [v for v in values if isinstance(v, bool)]
    if not values:
        return None, 0
    return sum(1 for v in values if v) / len(values), len(values)


def _mean(traces, field):
    values = [t.get("outcome", {}).get(field) for t in traces if isinstance(t.get("outcome"), dict)]
    values = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not values:
        return None, 0
    return sum(values) / len(values), len(values)


def compute_bucket(traces):
    repeated_error, n_re = _rate(traces, "repeated_error")
    resolved, n_res = _rate(traces, "resolved")
    escalated, n_esc = _rate(traces, "escalated")
    frustration, n_fr = _rate(traces, "frustration_signal")
    tokens, n_tok = _mean(traces, "tokens_used")
    calls, n_calls = _mean(traces, "llm_calls")
    return {
        "n_traces": len(traces),
        "repeated_error_rate": {"value": repeated_error, "n": n_re},
        "resolution_rate": {"value": resolved, "n": n_res},
        "escalation_rate": {"value": escalated, "n": n_esc},
        "frustration_rate": {"value": frustration, "n": n_fr},
        "avg_tokens_used": {"value": tokens, "n": n_tok},
        "avg_llm_calls": {"value": calls, "n": n_calls},
    }


def split_baseline(traces):
    baseline = [t for t in traces if isinstance(t.get("outcome"), dict) and t["outcome"].get("baseline") is True]
    baseline_ids = {id(t) for t in baseline}
    current = [t for t in traces if id(t) not in baseline_ids]
    return baseline, current


def _pct_delta(before, after, bounded_rate=False):
    if before is None or after is None:
        return None
    if before == 0:
        return (1.0 if after > 0 else 0.0) if bounded_rate else None
    return (after - before) / before


def fmt_pct(v, none="N/A"):
    return none if v is None else f"{v:.1%}"


def fmt_num(v, none="N/A"):
    return none if v is None else f"{v:.1f}"


def fmt_delta(v):
    if v is None:
        return "N/A"
    return f"{v:+.1%}"


_LABELS = [
    ("repeated_error_rate", "Repeated-error rate", "pct"),
    ("resolution_rate", "Resolution rate", "pct"),
    ("escalation_rate", "Escalation rate", "pct"),
    ("frustration_rate", "Frustration rate", "pct"),
    ("avg_tokens_used", "Avg. tokens used", "num"),
    ("avg_llm_calls", "Avg. LLM calls", "num"),
]


def render_markdown(report):
    out = []
    out.append("# CommonTrace Pilot Metrics Report")
    out.append("")
    out.append(f"**Date** : {report['timestamp']}")
    out.append(f"**Schema version** : {report['schema_version']}")
    out.append(f"**Total traces** : {report['n_traces_total']}")
    out.append("")
    out.append(
        "The five business-outcome metrics, computed from "
        "`Trace.outcome` fields. See protocol/PROTOCOL.md#11-pilot-outcome-metrics."
    )
    out.append("")

    baseline = report["baseline"]
    current = report["current"]
    has_baseline = baseline["n_traces"] > 0

    if has_baseline:
        out.append(f"## Baseline vs. current ({baseline['n_traces']} baseline / {current['n_traces']} current traces)")
        out.append("")
        out.append("| Metric | Baseline | Current | Change |")
        out.append("|---|---|---|---|")
        for key, label, kind in _LABELS:
            b = baseline[key]["value"]
            c = current[key]["value"]
            fmt = fmt_pct if kind == "pct" else fmt_num
            delta = _pct_delta(b, c, bounded_rate=(kind == "pct"))
            out.append(f"| {label} | {fmt(b)} | {fmt(c)} | {fmt_delta(delta)} |")
        out.append("")
        out.append(
            "*Change is relative: -53% means the current value is 53% lower "
            "than baseline.*"
        )
    else:
        out.append(f"## Current ({current['n_traces']} traces)")
        out.append("")
        out.append(
            "*No traces marked `outcome.baseline: true` yet — nothing to compare "
            "against. Capture some baseline traces (`commontrace capture --baseline "
            "...`) before deploying lesson injection to get before/after deltas.*"
        )
        out.append("")
        out.append("| Metric | Value | n |")
        out.append("|---|---|---|")
        for key, label, kind in _LABELS:
            fmt = fmt_pct if kind == "pct" else fmt_num
            entry = current[key]
            out.append(f"| {label} | {fmt(entry['value'])} | {entry['n']} |")
    out.append("")
    return "\n".join(out)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Pilot business-outcome metrics for CommonTrace (repeated-error, "
        "resolution, escalation, frustration rate + token/LLM-call cost).",
    )
    parser.add_argument("--dest", default=None, help="Store root (default: $COMMONTRACE_ROOT or repo checkout)")
    parser.add_argument("--agent-type", default=None, help="Filter to one agent_type")
    parser.add_argument("--json", action="store_true", help="Raw JSON output to stdout")
    parser.add_argument("--html", action="store_true", help="Output HTML to memory/benchmark_reports/")
    args = parser.parse_args()
    if args.json and args.html:
        parser.error("--json and --html are mutually exclusive (choose one output format).")

    root = args.dest or _ROOT
    traces = load_traces(root, args.agent_type)

    if not traces:
        if args.json:
            import json
            print(json.dumps({
                "error": "no_traces",
                "message": "No traces with outcome data found. Run `commontrace capture` with outcome flags first.",
            }))
        else:
            print("No traces with outcome data found. Run `commontrace capture` with outcome flags first.")
        sys.exit(0)

    baseline_traces, current_traces = split_baseline(traces)

    report = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "n_traces_total": len(traces),
        "baseline": compute_bucket(baseline_traces),
        "current": compute_bucket(current_traces),
    }

    if args.json:
        import json
        clean = dict(report)
        print(json.dumps(clean, indent=2, default=str))
        return

    md = render_markdown(report)
    if args.html:
        html_report = mp.render_html(md, report["timestamp"])
        out_dir = os.path.join(root, "memory", "benchmark_reports")
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = os.path.join(out_dir, f"pilot_{ts}.html")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(html_report)
        print(f"HTML report written: {out_path}")
    else:
        print(md)


if __name__ == "__main__":
    main()
