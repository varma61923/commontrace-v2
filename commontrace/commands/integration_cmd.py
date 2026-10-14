"""Print native MCP client configuration without modifying application settings."""
from __future__ import annotations

import argparse
import json
from typing import Any

from commontrace import paths

CLIENTS = ("claude-desktop", "cursor", "windsurf", "zed", "vscode", "jetbrains")


def configuration(
    client: str, root: str, *, command: str = "commontrace", approval: bool = False
) -> dict[str, Any]:
    if client not in CLIENTS:
        raise ValueError("unsupported MCP client")
    if not command or len(command) > 4096 or any(char in command for char in "\r\n\x00"):
        raise ValueError("command must be one executable path, without control characters")
    args = ["serve", "--dest", paths.resolve_root(root)]
    if not approval:
        args.append("--no-approval")
    if client == "zed":
        return {"context_servers": {"commontrace": {"command": {"path": command, "args": args, "env": {}}}}}
    server = {"command": command, "args": args}
    if client == "vscode":
        return {"servers": {"commontrace": {"type": "stdio", **server}}}
    return {"mcpServers": {"commontrace": server}}


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("integration", help="Print a native, local MCP client configuration fragment.")
    parser.add_argument("--client", required=True, choices=CLIENTS)
    parser.add_argument("--dest", default=None, help="Explicit store root; no files are changed")
    parser.add_argument("--command", default="commontrace", help="Installed executable path (not a shell command)")
    parser.add_argument("--approval", action="store_true", help="Include lesson approval tools; disabled by default")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    print(json.dumps(configuration(args.client, paths.resolve_root(args.dest),
                                   command=args.command, approval=args.approval), indent=2))
    return 0
