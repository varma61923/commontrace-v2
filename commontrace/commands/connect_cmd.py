"""Owner-scoped, read-only knowledge provider syncs."""
from __future__ import annotations

import json
import os
import sys

from commontrace import paths
from commontrace.connectors.knowledge import PROVIDERS, sync


def add_parser(subparsers):
    parser = subparsers.add_parser("connect", help="Read knowledge from provider APIs into a pinned memory scope.")
    parser.add_argument("provider", choices=sorted(PROVIDERS))
    parser.add_argument("resource")
    parser.add_argument("--token-env", required=True, help="Environment variable containing the provider OAuth token.")
    parser.add_argument("--context", action="append", required=True)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--dest")
    parser.set_defaults(func=run)


def run(args):
    try:
        result = sync(paths.resolve_root(args.dest), args.provider, args.resource,
                      token=os.environ.get(args.token_env, ""), context=args.context, max_pages=args.max_pages)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, PermissionError):
        print("Provider sync failed; its watermark was not advanced.", file=sys.stderr)
        return 1
