"""Owner-scoped, read-only knowledge provider syncs."""
from __future__ import annotations

import json
import math
import sys
import threading

from commontrace import paths
from commontrace.connectors import knowledge_streams
from commontrace.connectors.knowledge import PROVIDERS, sync
from commontrace.secrets_provider import env_secret


def add_parser(subparsers):
    parser = subparsers.add_parser("connect", help="Read knowledge from provider APIs into a pinned memory scope.")
    parser.add_argument("provider", choices=sorted([*PROVIDERS, *knowledge_streams.STREAMS]))
    parser.add_argument("resource")
    parser.add_argument("--token-env", required=True, help="Environment variable containing the provider OAuth token.")
    parser.add_argument("--context", action="append", required=True)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--account", default="", help="Connection/account name (required for streams).")
    parser.add_argument("--ref", default="HEAD", help="GitHub repository commit or reference.")
    parser.add_argument("--watch", action="store_true", help="Poll completed syncs until interrupted.")
    parser.add_argument("--interval", type=float, default=60, help="Polling interval, 1-86400 seconds.")
    parser.add_argument("--webhook-secret-env", help="Configure the local GitHub push webhook with this secret name.")
    parser.add_argument("--dest")
    parser.set_defaults(func=run)


def run(args):
    try:
        if not math.isfinite(args.interval) or not 1 <= args.interval <= 86400:
            raise ValueError("polling interval must be in 1..86400")
        root = paths.resolve_root(args.dest)
        if args.webhook_secret_env:
            if args.provider != "github-repo":
                raise ValueError("webhooks require github-repo")
            knowledge_streams.configure_github_webhook(root, args.resource, token_env=args.token_env,
                secret_env=args.webhook_secret_env, context=args.context, account=args.account)
        stop = threading.Event()
        while True:
            token = env_secret(args.token_env)
            result = (knowledge_streams.sync(root, args.provider, args.resource, token=token, context=args.context,
                        max_pages=args.max_pages, account=args.account, ref=args.ref)
                      if args.provider in knowledge_streams.STREAMS else
                      sync(root, args.provider, args.resource, token=token, context=args.context,
                           max_pages=args.max_pages))
            print(json.dumps(result, indent=2), flush=True)
            if not args.watch or stop.wait(args.interval):
                return 0
    except (ValueError, OSError, PermissionError):
        print("Provider sync failed; its watermark was not advanced.", file=sys.stderr)
        return 1
