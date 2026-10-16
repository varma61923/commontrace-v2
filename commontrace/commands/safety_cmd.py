"""Explicit hazard review and safety-evidence export."""
from __future__ import annotations

import argparse
import json

from commontrace import frontier_protocol as fp
from commontrace import paths, safety


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("safety", help="Protect hazards, inspect indicators and export safety evidence.")
    sub = parser.add_subparsers(dest="operation", required=True)
    mark = sub.add_parser("mark")
    mark.add_argument("id")
    mark.add_argument("--class", dest="hazard_class", choices=sorted(safety.CLASSES), required=True)
    mark.add_argument("--reviewer", required=True)
    mark.add_argument("--reason", required=True)
    mark.add_argument("--site", default="")
    mark.add_argument("--robot", default="")
    spi = sub.add_parser("spi")
    spi.add_argument("episodes", help="JSON episode summaries with timestamp, hours and intervention counts.")
    recorder = sub.add_parser("recorder")
    recorder.add_argument("action", choices=("export",))
    for child in (mark, spi, recorder):
        child.add_argument("--dest")
        child.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    if args.operation == "mark":
        review = safety.review(root, args.id, reviewer=args.reviewer, reason=args.reason)
        result = safety.mark(root, args.id, args.hazard_class, review, site=args.site, robot=args.robot)
    elif args.operation == "spi":
        with open(args.episodes, "rb") as stream:
            raw = stream.read(fp.MAX_BYTES + 1)
        if len(raw) > fp.MAX_BYTES:
            raise ValueError("SPI request exceeds 1 MiB")
        result = safety.spi(json.loads(raw))
    else:
        result = safety.export(root)
    print(json.dumps(result, indent=2))
    return 0
