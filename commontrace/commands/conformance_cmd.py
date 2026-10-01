"""`commontrace conformance`: check an implementation against the protocol. See commontrace/conformance.py."""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import conformance, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "conformance", help="Check an implementation against the protocol: vectors, a store, or a gateway.")
    sub = p.add_subparsers(dest="conformance_cmd", required=True)
    v = sub.add_parser("vectors", help="Write or check the reference vectors (protocol/conformance/vectors.json).")
    v.add_argument("--write", action="store_true", help="Regenerate the file from the reference implementation.")
    v.set_defaults(func=run_vectors)
    e = sub.add_parser("exec", help="Run your program against the vectors over stdio (one JSON object per line).")
    e.add_argument("command", help='e.g. "./my-impl" or "python3 -m commontrace.conformance"')
    e.add_argument("--vectors", default=None)
    e.add_argument("--only", default=None, help="Comma-separated ops to check (assign,ledger,digest,revision): "
                                                "an implementation may cover a subset, and the report says which.")
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=run_exec)
    s = sub.add_parser("store", help="Check a local store's files and assignments.")
    s.add_argument("--dest", default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=run_store)
    g = sub.add_parser("gateway", help="Drive a running gateway's HTTP API and check its behaviour.")
    g.add_argument("url")
    g.add_argument("--token", required=True)
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=run_gateway)


def _emit(results, as_json: bool) -> int:
    print(json.dumps(conformance.to_dict(results), indent=2) if as_json else conformance.render(results))
    return 0 if all(r.ok for r in results) else 1


def run_vectors(args: argparse.Namespace) -> int:
    if args.write:
        text = json.dumps(conformance.build_vectors(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"
        for path in (conformance.VECTORS_PATH, conformance.SPEC_VECTORS_PATH):
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            print(f"wrote {path}")
        return 0
    return _emit(conformance.check_vectors_against_reference(conformance.load_vectors()), False)


def run_exec(args: argparse.Namespace) -> int:
    only = tuple(x.strip() for x in args.only.split(",")) if args.only else None
    if only and set(only) - {"assign", "ledger", "digest", "revision"}:
        print("[commontrace] --only takes assign, ledger, digest, revision", file=sys.stderr)
        return 2
    return _emit(conformance.run_exec(args.command, conformance.load_vectors(args.vectors), only=only), args.json)


def run_store(args: argparse.Namespace) -> int:
    return _emit(conformance.check_store(paths.resolve_root(args.dest)), args.json)


def run_gateway(args: argparse.Namespace) -> int:
    results = conformance.check_gateway(args.url, args.token)
    if results and results[0].detail.startswith("HTTP 0"):
        print(f"[commontrace] cannot reach {args.url}", file=sys.stderr)
    return _emit(results, args.json)
