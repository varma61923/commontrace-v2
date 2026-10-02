from __future__ import annotations

import argparse
import json
import sys

from commontrace import failure_signals, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "signals",
        help="Cluster this store's failing traces into named signals, and export one "
        "as a regression dataset for LangSmith/Braintrust.",
    )
    sub = p.add_subparsers(dest="signals_cmd", required=True)

    ls = sub.add_parser("list", help="List failure signals found in this store.")
    ls.add_argument("--agent-type", default=None)
    ls.add_argument("--similarity-threshold", type=float, default=failure_signals.DEFAULT_SIMILARITY,
                    help="Cosine similarity at which failures are one signal (average linkage, IDF-weighted).")
    ls.add_argument("--min-cluster-size", type=int, default=2)
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--dest", default=None)
    ls.set_defaults(func=run_list)

    exp = sub.add_parser(
        "export", help="Export one signal's occurrences as a regression dataset.",
    )
    exp.add_argument("name", help="A signal's exact name, from `signals list`.")
    exp.add_argument("--format", choices=("langsmith", "braintrust"), required=True)
    exp.add_argument("--agent-type", default=None)
    exp.add_argument("--similarity-threshold", type=float, default=failure_signals.DEFAULT_SIMILARITY)
    exp.add_argument("--min-cluster-size", type=int, default=2)
    exp.add_argument("--out", default=None, help="Output file. Default: stdout.")
    exp.add_argument("--dest", default=None)
    exp.set_defaults(func=run_export)


def _build(args: argparse.Namespace, root: str):
    return failure_signals.build_signals(
        root, agent_type=args.agent_type,
        similarity_threshold=args.similarity_threshold, min_cluster_size=args.min_cluster_size,
    )


def run_list(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    signals, _by_id = _build(args, root)
    if not signals:
        print("[commontrace] no failure signals found (no traces recorded as a failure, "
              "or none repeat enough to cluster).")
        return 0
    if args.json:
        print(json.dumps([
            {
                "name": s.name, "size": s.size, "trend": s.trend,
                "affected_agents": s.affected_agents,
                "first_seen": s.first_seen, "last_seen": s.last_seen,
                "trace_ids": s.trace_ids,
            }
            for s in signals
        ], indent=2))
        return 0
    for s in signals:
        agents = ", ".join(s.affected_agents) if s.affected_agents else "(not recorded)"
        print(f"  {s.name}")
        print(f"    size={s.size} trend={s.trend} agents={agents}")
        if s.first_seen or s.last_seen:
            print(f"    first_seen={s.first_seen or '?'} last_seen={s.last_seen or '?'}")
    return 0


def run_export(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    signals, by_id = _build(args, root)
    match = next((s for s in signals if s.name == args.name), None)
    if match is None:
        print(
            f"[commontrace] no signal named {args.name!r}. Run `commontrace signals list` "
            "for the exact names currently found.",
            file=sys.stderr,
        )
        return 1

    rows = (
        failure_signals.export_langsmith(match, by_id) if args.format == "langsmith"
        else failure_signals.export_braintrust(match, by_id)
    )
    text = "\n".join(json.dumps(r) for r in rows) + "\n"

    if args.out:
        try:
            safe_out = paths.safe_prepare_output_path(args.out)
        except (OSError, ValueError) as exc:
            print(f"[commontrace] could not write {args.out!r}: {exc}", file=sys.stderr)
            return 1
        with open(safe_out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"[commontrace] wrote {len(rows)} example(s) to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0
