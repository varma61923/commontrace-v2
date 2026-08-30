"""`commontrace account` — self-service deletion against a CommonTrace Hub.

Two different risk shapes, two different flows:

`delete-trace` removes one of your own traces (and its full amendment
chain) immediately. Irreversible, but scoped to one trace -- not
categorically riskier than what a compromised key can already do via
`amend`, which is why it takes effect in a single call.

`request-deletion` / `confirm-deletion` remove your ENTIRE organization --
every trace, vote, api key, and Knowledge Base submission. That is
deliberately NOT a single call: `request-deletion` deletes nothing, it
only returns a one-time confirmation token and a minimum wait before that
token can be used. `confirm-deletion` needs the token AND the wait to have
elapsed. The gap exists so a single compromised API key cannot wipe an
org's history with no window for anyone to notice -- see
hub/crud.py:request_org_deletion for the full reasoning. `cancel-deletion`
stands down a pending request at any point before it is confirmed.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from commontrace import hub_client
from commontrace.commands import _format

_resolve_hub = _format.resolve_hub


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "account",
        help="Self-service deletion against a Hub: one trace, or your entire organization.",
    )
    sub = p.add_subparsers(dest="account_cmd", required=True)

    delete_trace = sub.add_parser(
        "delete-trace",
        help="Permanently delete one of your own traces (and its amendment chain).",
    )
    delete_trace.add_argument("trace_id")
    delete_trace.add_argument("--yes", action="store_true", help="Skip the interactive confirmation.")
    delete_trace.add_argument("--hub-url", default=None, help="Default: $COMMONTRACE_HUB_URL")
    delete_trace.add_argument("--hub-api-key", default=None, help="Default: $COMMONTRACE_HUB_API_KEY")
    delete_trace.set_defaults(func=run_delete_trace)

    request = sub.add_parser(
        "request-deletion",
        help="Start permanently deleting your ENTIRE organization. Deletes nothing yet.",
    )
    request.add_argument("--hub-url", default=None)
    request.add_argument("--hub-api-key", default=None)
    request.set_defaults(func=run_request_deletion)

    cancel = sub.add_parser(
        "cancel-deletion",
        help="Cancel a pending request-deletion request.",
    )
    cancel.add_argument("--hub-url", default=None)
    cancel.add_argument("--hub-api-key", default=None)
    cancel.set_defaults(func=run_cancel_deletion)

    confirm = sub.add_parser(
        "confirm-deletion",
        help="Finish deleting your entire organization. Irreversible.",
    )
    confirm.add_argument("confirmation_token")
    confirm.add_argument("--yes", action="store_true", help="Skip the interactive confirmation.")
    confirm.add_argument("--hub-url", default=None)
    confirm.add_argument("--hub-api-key", default=None)
    confirm.set_defaults(func=run_confirm_deletion)


def _confirm_destructive(action: str, skip: bool) -> bool:
    if skip:
        return True
    if not sys.stdin.isatty():
        print(
            f"[commontrace] refusing to {action} without --yes (stdin is not a terminal, "
            "cannot prompt for confirmation).",
            file=sys.stderr,
        )
        return False
    reply = input(f"[commontrace] this will {action.upper()}. This cannot be undone. Type 'yes' to continue: ")
    return reply.strip().lower() == "yes"


def run_delete_trace(args: argparse.Namespace) -> int:
    if not _confirm_destructive(f"permanently delete trace {args.trace_id}", args.yes):
        print("[commontrace] aborted (no changes made).", file=sys.stderr)
        return 1

    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved

    try:
        deleted = asyncio.run(hub_client.delete_trace(hub_url, api_key, args.trace_id))
    except (hub_client.HubClientUnavailable, hub_client.HubConnectionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    if not deleted:
        print(f"[commontrace] no such trace: {args.trace_id}", file=sys.stderr)
        return 1
    print(f"[commontrace] permanently deleted trace: {args.trace_id}")
    return 0


def run_request_deletion(args: argparse.Namespace) -> int:
    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved

    try:
        result = asyncio.run(hub_client.request_account_deletion(hub_url, api_key))
    except (hub_client.HubClientUnavailable, hub_client.HubConnectionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print("[commontrace] deletion requested. NOTHING has been deleted yet.")
    print(f"  confirmation token: {result['confirmation_token']}")
    print(f"  earliest you may confirm: {result['confirm_not_before']}")
    print(f"  token expires: {result['expires_at']}")
    print(
        "  Save this token somewhere other than shell history if this org's history "
        "matters to you -- `commontrace account confirm-deletion <token>` will "
        "PERMANENTLY delete every trace, vote, api key, and Knowledge Base "
        "submission this organization has. `commontrace account cancel-deletion` "
        "stands this down instead."
    )
    return 0


def run_cancel_deletion(args: argparse.Namespace) -> int:
    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved

    try:
        cancelled = asyncio.run(hub_client.cancel_account_deletion(hub_url, api_key))
    except (hub_client.HubClientUnavailable, hub_client.HubConnectionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    if not cancelled:
        print("[commontrace] no pending deletion request to cancel.")
        return 0
    print("[commontrace] pending deletion request cancelled. Nothing was deleted.")
    return 0


def run_confirm_deletion(args: argparse.Namespace) -> int:
    if not _confirm_destructive("permanently delete this ENTIRE organization", args.yes):
        print("[commontrace] aborted (no changes made).", file=sys.stderr)
        return 1

    resolved = _resolve_hub(args)
    if resolved is None:
        return 1
    hub_url, api_key = resolved

    try:
        asyncio.run(hub_client.confirm_account_deletion(hub_url, api_key, args.confirmation_token))
    except (hub_client.HubClientUnavailable, hub_client.HubConnectionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print("[commontrace] organization permanently deleted.")
    return 0
