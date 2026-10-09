"""Vendor JSON exports -> external evidence and review drafts."""
from __future__ import annotations

import json

from commontrace import migration, paths
from commontrace.ingest import _open_regular


def add_parser(subparsers):
    parser = subparsers.add_parser("migrate", help="Import a Mem0, Letta, Zep or Graphiti JSON export safely.")
    parser.add_argument("vendor", choices=migration.VENDORS)
    parser.add_argument("file")
    parser.add_argument("--context", action="append", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dest")
    parser.set_defaults(func=run)


def run(args):
    with _open_regular(args.file, 32*1024*1024) as stream:
        document = json.loads(stream.read(32*1024*1024+1))
    print(json.dumps(migration.migrate(paths.resolve_root(args.dest), args.vendor, document,
                                      context=args.context, dry_run=args.dry_run), indent=2))
    return 0
