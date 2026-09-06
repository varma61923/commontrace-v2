#!/usr/bin/env python3
"""pilot_metrics.py — CommonTrace's five business-outcome metrics.

Computes, from memory/traces/*.md `outcome` frontmatter (see
protocol/schemas/trace.schema.json and protocol/PROTOCOL.md#11-pilot-outcome-metrics):

- repeated_error_rate : % of traces marking a recurrence of a known failure
- resolution_rate     : % of traces whose task reached a successful conclusion
- escalation_rate     : % of traces that required human escalation
- frustration_rate    : % of traces with an explicit negative signal
- avg_tokens_used / avg_llm_calls : mean cost per trace

Unlike benchmark/measure_performance.py (which measures whether the protocol
*machinery* is healthy — Omega/Alpha quality), this measures whether the
*fleet's behavior* actually changed, split baseline (outcome.baseline: true)
vs current. Intended to be run continuously against a production fleet, not
only during an initial evaluation window: the same before/after split answers
"did adopting this help?" on day 30 and "is it still helping?" on day 300.

Usage:
    python pilot_metrics.py                 # markdown stdout, all traces
    python pilot_metrics.py --json          # raw JSON to stdout
    python pilot_metrics.py --html          # HTML to memory/benchmark_reports/
    python pilot_metrics.py --dest /path    # override store root

Any agent_type may be filtered with --agent-type.
"""
import argparse
import datetime
import glob
import os
import re
import sys

# Reuse the frontmatter parser + HTML renderer from the sibling benchmark script.
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
    """Fraction of traces where outcome[field] is True, over traces where it's a real bool."""
    values = [t.get("outcome", {}).get(field) for t in traces if isinstance(t.get("outcome"), dict)]
    # Strictly bool, not just truthy -- a malformed/hand-edited frontmatter value like the
    # string "false" is truthy in Python and would otherwise invert the rate.
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
    # `is True`, not truthiness: trace.schema.json's `baseline` is a bool
    # field, but a hand-edited trace is free to write `baseline: "false"`
    # (a non-empty string, which is truthy in Python) and truthiness alone
    # would misclassify that trace as baseline data -- silently mixing a
    # CURRENT trace into the pre-CommonTrace baseline bucket it explicitly
    # says it is not.
    baseline = [t for t in traces if isinstance(t.get("outcome"), dict) and t["outcome"].get("baseline") is True]
    # Identity, not equality. `t not in baseline` is an O(n) dict comparison
    # per trace -- O(n^2) overall with a full field-by-field compare at each
    # step -- and it is correct today only because load_traces happens to set
    # fm["_path"], making every dict unique. That is a load-bearing side
    # effect of an unrelated line: drop or move `_path` and two traces with
    # identical content would silently collapse into one.
    baseline_ids = {id(t) for t in baseline}
    current = [t for t in traces if id(t) not in baseline_ids]
    return baseline, current


def _pct_delta(before, after, bounded_rate=False):
    """Relative change from before -> after, as a signed fraction (e.g. -0.53 = -53%).

    A true relative change from a zero baseline is undefined (division by
    zero) for an unbounded quantity like avg_tokens_used -- "0 -> 500
    tokens" has no meaningful percentage, so that case stays None/"N/A".
    `bounded_rate=True` is for the four [0, 1] rate metrics specifically
    (resolution_rate and friends): a genuine 0% -> positive-% improvement
    is the maximal positive signal a bounded rate can report, exactly like
    commontrace/commands/pilot_cmd.py's identical handling for the same
    metric family -- without this, `commontrace bench --pilot` reported
    "N/A" for a 0% -> 80% resolution-rate improvement while `commontrace
    pilot` reported "+100%" for the identical underlying numbers, out of
    the same trace store.
    """
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
