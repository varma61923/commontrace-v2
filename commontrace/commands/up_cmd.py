"""One-command local server with the same scoped HTTP memory API."""
from __future__ import annotations

from commontrace import cli, paths


def add_parser(subparsers):
    parser = subparsers.add_parser("up", help="Initialize a store and run its local memory API and console.")
    parser.add_argument("--dest", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.set_defaults(func=run)


def run(args):
    root = paths.resolve_root(args.dest)
    result = cli.main(["init", "--dest", root])
    if result:
        return result
    return cli.main(["gateway", "--dest", root, "--host", args.host, "--port", str(args.port)])
