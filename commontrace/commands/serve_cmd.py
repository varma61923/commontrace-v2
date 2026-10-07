"""`commontrace serve` -- the local store over SDK MCP transports."""
from __future__ import annotations

import argparse
import sys

from commontrace import paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "serve",
        help="Serve this store over MCP stdio, SSE, or Streamable HTTP.",
        description=(
            "Expose the local store as an MCP server on stdin/stdout, so an agent "
            "with no terminal can retrieve, capture, and curate its own memory. "
            "Run by the MCP client, not by hand -- `commontrace install --target generic-mcp` "
            "writes the config entry that launches it."
        ),
    )
    p.add_argument("--dest", default=None,
                   help="Store root (default: auto-detect / $COMMONTRACE_ROOT)")
    p.add_argument("--transport", choices=("stdio", "sse", "streamable-http"), default="stdio")
    p.add_argument("--host", default="127.0.0.1", help="HTTP listener address (default: loopback)")
    p.add_argument("--port", type=int, default=8421, help="HTTP listener port")
    p.add_argument("--allowed-host", action="append", default=[],
                   help="Allowed HTTP Host, e.g. memory.example.com; required for remote listeners")
    p.add_argument("--allowed-origin", action="append", default=[], help="Allowed browser Origin")
    p.add_argument("--tls-cert", default=None, help="PEM certificate for HTTPS")
    p.add_argument("--tls-key", default=None, help="Private key for HTTPS")
    p.add_argument("--allow-insecure-http", action="store_true",
                   help="Permit non-loopback HTTP behind an operator-managed TLS reverse proxy")
    p.add_argument("--max-connections", type=int, default=128, help="Maximum active HTTP requests/streams")
    p.add_argument("--max-sessions", type=int, default=128, help="Maximum Streamable HTTP sessions")
    p.add_argument("--session-idle-timeout", type=float, default=300, help="Idle session timeout in seconds")
    p.add_argument(
        "--no-approval", dest="allow_approval", action="store_false", default=True,
        help="Omit approve_lesson/reject_lesson entirely, for a deployment where "
             "activating a lesson must go through a person. The tools are absent "
             "from the listing, not merely refused, so the agent never plans around "
             "a call it cannot make.",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    from commontrace import mcp_server

    try:
        if args.transport == "stdio":
            print(f"[commontrace] serving {root} over MCP stdio"
                  + ("" if args.allow_approval else " (approval tools disabled)"), file=sys.stderr)
            return mcp_server.serve(root, allow_approval=args.allow_approval)
        if not 0 <= args.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if bool(args.tls_cert) != bool(args.tls_key):
            raise ValueError("TLS requires both --tls-cert and --tls-key")
        if args.host not in ("127.0.0.1", "localhost", "::1") and not args.tls_cert and not args.allow_insecure_http:
            raise ValueError("a remote listener requires TLS or explicit --allow-insecure-http")
        from commontrace.mcp_transport import build_http_app

        app = build_http_app(
            root, transport=args.transport, host=args.host, allow_approval=args.allow_approval,
            allowed_hosts=args.allowed_host, allowed_origins=args.allowed_origin,
            max_connections=args.max_connections, max_sessions=args.max_sessions,
            session_idle_timeout=args.session_idle_timeout,
        )
        import uvicorn

        print(f"[commontrace] serving MCP {args.transport} on {args.host}:{args.port}; "
              "bearer credential managed by commontrace gateway --rotate-token/--revoke-token", file=sys.stderr)
        uvicorn.run(app, host=args.host, port=args.port, ssl_certfile=args.tls_cert, ssl_keyfile=args.tls_key,
                    access_log=False)
        return 0
    except (mcp_server.LocalStoreError, ValueError, OSError, ImportError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
