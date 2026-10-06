"""`commontrace gateway`: the causal loop over plain JSON, for any agent or robot."""
from __future__ import annotations

import argparse
import sys

from commontrace import gateway, gateway_tokens, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "gateway",
        help="Serve the causal loop over HTTP+JSON (or JSON lines on stdio) to any agent, "
             "in any language -- an LLM, a ROS node, a C++ planner. Also serves the local console.",
        description=gateway.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dest", default=None, help="Store root (default: auto-detect / $COMMONTRACE_ROOT)")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default: loopback).")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--stdio", action="store_true",
                   help="Serve one JSON object per line on stdin/stdout instead of HTTP.")
    p.add_argument("--token", default=None,
                   help="Bearer token (default: one generated into memory/gateway.token).")
    token_action = p.add_mutually_exclusive_group()
    token_action.add_argument("--rotate-token", action="store_true",
                              help="Atomically replace the file-backed token and exit; running gateways reload it.")
    token_action.add_argument("--revoke-token", action="store_true",
                              help="Revoke the file-backed token and exit; running gateways reject it immediately.")
    p.add_argument("--token-ttl", type=float, default=None, metavar="SECONDS",
                   help="Lifetime for a newly created or rotated file-backed token; default: no expiry.")
    p.add_argument("--show-token", action="store_true",
                   help="Print the bearer token in the console URL (otherwise kept out of logs).")
    p.add_argument("--env", default=None,
                   help="The one environment this store measures (sim, real, ...). Never pooled.")
    p.add_argument("--protect", action="append", default=[], metavar="PREFIX",
                   help="Item ids starting with PREFIX are safety constraints: always delivered, "
                        "never withheld, withdrawn or counted. Repeatable; stored with the store.")
    p.add_argument("--relaxed-durability", action="store_true",
                   help="Skip fsync on each log line (control loops on flash). A power loss can "
                        "drop the last few lines.")
    p.add_argument("--on-harm", choices=("inform", "withdraw"), default=None,
                   help="Override the store's harm policy for this gateway.")
    p.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                   help="Extra Host header names to answer to (needed when binding beyond loopback).")
    p.add_argument("--allow-approval", action="store_true",
                   help="Let the console edit, approve and reject lessons in review (off by default: approving "
                        "changes what every agent is told).")
    p.add_argument("--tls-cert", default=None)
    p.add_argument("--tls-key", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        if args.token and (args.rotate_token or args.revoke_token or args.token_ttl is not None):
            raise ValueError("token rotation, revocation and ttl apply to file-backed tokens, not --token")
        if args.rotate_token:
            token = gateway_tokens.rotate_token(root, ttl=args.token_ttl)
            print(f"[commontrace] gateway token rotated in {gateway.token_path(root)}", file=sys.stderr)
            if args.show_token:
                print(token)
            return 0
        if args.revoke_token:
            gateway_tokens.revoke_token(root)
            print("[commontrace] gateway token revoked", file=sys.stderr)
            return 0
        config = gateway.merge_config(root, env=args.env, protect=args.protect)
        token = None if args.stdio else (args.token or gateway.load_or_create_token(root, ttl=args.token_ttl))
    except (ValueError, OSError) as exc:
        print(f"[commontrace] error: {exc}", file=sys.stderr)
        return 2
    gw = gateway.Gateway(
        root, token=token,
        config=config, durable=not args.relaxed_durability, on_harm=args.on_harm,
        allowed_hosts=tuple(args.allow_host), allow_approval=args.allow_approval,
        token_provider=gateway_tokens.FileTokenProvider(root) if not args.stdio and not args.token else None)
    if args.stdio:
        print(f"[commontrace] gateway on stdio for {root}", file=sys.stderr)
        return gateway.serve_stdio(gw, sys.stdin, sys.stdout)

    if bool(args.tls_cert) != bool(args.tls_key):
        print("[commontrace] error: --tls-cert and --tls-key go together", file=sys.stderr)
        return 2
    loopback = args.host in ("127.0.0.1", "::1", "localhost")
    if not loopback and not args.tls_cert:
        print("[commontrace] warning: binding beyond loopback without TLS sends the bearer token in "
              "the clear; terminate TLS in front of this or pass --tls-cert/--tls-key.", file=sys.stderr)
    if not loopback and args.host not in ("0.0.0.0", "::"):  # nosec B104 - a comparison, not a bind
        gw.allowed_hosts = gw.allowed_hosts | {args.host.lower()}
    try:
        server = gateway.make_http_server(
            gw, args.host, args.port,
            tls=(args.tls_cert, args.tls_key) if args.tls_cert else None)
    except OSError as exc:
        print(f"[commontrace] cannot listen on {args.host}:{args.port}: {exc.strerror or exc}",
              file=sys.stderr)
        return 1
    scheme = "https" if args.tls_cert else "http"
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host  # nosec B104 - display only
    port = server.server_address[1]
    print(f"[commontrace] gateway for {root}"
          + (f" (environment: {config.env})" if config.env else "")
          + (f", protecting {', '.join(config.protected_prefixes)}" if config.protected_prefixes else "")
          + ("" if gw.durable else ", relaxed durability"), file=sys.stderr)
    print(f"  API:      {scheme}://{shown}:{port}/v1/openapi.json   (Authorization: Bearer <token>)",
          file=sys.stderr)
    console = f"{scheme}://{shown}:{port}/"
    if args.show_token:
        console += f"#token={gw.token}"
    print(f"  Console:  {console}", file=sys.stderr)
    print(f"  Token in: {gateway.token_path(root)}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
