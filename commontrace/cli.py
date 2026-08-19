from __future__ import annotations

import argparse
import sys

from commontrace import PROTOCOL_VERSION, __version__
from commontrace.commands import (
    bench_cmd,
    capture_cmd,
    doctor_cmd,
    index_cmd,
    init_cmd,
    install_cmd,
    lesson_cmd,
    query_cmd,
    sync_cmd,
    trace_cmd,
)
from commontrace.frontmatter import FrontmatterError

_SUBCOMMANDS = [
    init_cmd,
    install_cmd,
    capture_cmd,
    trace_cmd,
    lesson_cmd,
    query_cmd,
    index_cmd,
    bench_cmd,
    sync_cmd,
    doctor_cmd,
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="commontrace",
        description="CommonTrace Protocol client - capture experience, curate lessons, "
        "inject them into any agent platform. Spec: protocol/PROTOCOL.md",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"commontrace {__version__} (protocol {PROTOCOL_VERSION})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for module in _SUBCOMMANDS:
        module.add_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FrontmatterError as exc:
        print(f"[commontrace] error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
