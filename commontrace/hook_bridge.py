"""Stdlib-only native transport, also launched directly with Python -I -S.

Admission, normalization, capture and memory selection belong to HookKit's
broker. This client only frames requests, formats replies and confirms delivery.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time

EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "PreCompact", "SubagentStart", "SubagentStop", "Stop", "SessionEnd",
)
AGENTS = ("claude", "codex", "cursor", "cline", "gemini", "kimi", "windsurf", "openhands")
FRAME_LIMIT = 100000
RESPONSE_SECONDS = 0.025
RECEIPT_SECONDS = 0.020


def format_reply(agent: str | None, event: str, reply: dict) -> dict | str:
    reply = {k: v for k, v in reply.items() if k != "delivery_token"}
    context = reply.get("context", "")
    if agent == "cline":
        return {"cancel": False, "contextModification": context}
    if agent == "openhands":
        return {"decision": "allow", "additionalContext": context}
    if agent == "windsurf":
        return {}  # Capture only; stdout is UI output rather than model context.
    if agent == "kimi":
        return context
    if agent == "cursor":
        return {"additional_context": context}
    if agent in ("claude", "codex", "gemini") and context:
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
    return reply


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)  # Invalid host input must not fail the host's tool call.


def _emit(agent, event, reply):
    output = format_reply(agent, event, reply)
    encoded = ((output if isinstance(output, str) else json.dumps(output)) + "\n").encode("utf-8")
    # A buffered print could appear successful before a broken pipe is detected.
    # Confirm every byte reaches stdout before sending a delivery receipt.
    while encoded:
        written = os.write(sys.stdout.fileno(), encoded)
        if not written:
            raise OSError("stdout closed")
        encoded = encoded[written:]


def _remaining(client, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("native hook deadline")
    client.settimeout(remaining)


def _capture(args, payload):
    # The inherited anonymous file avoids blocking on a cold child's stdin pipe.
    # Its bytes never enter argv; the detached process owns the remaining handle.
    import subprocess
    import tempfile

    with tempfile.TemporaryFile() as stream:
        stream.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        stream.seek(0)
        subprocess.Popen(  # noqa: S603 - interpreter and argv are generated, never a shell
            [args.python or sys.executable, "-I", "-m", "commontrace.hook_kit", "--capture-only",
             "--dest", args.dest, "--agent-id", args.agent_id, "--scope", args.scope,
             "--event", args.event, *(["--format", args.format] if args.format else [])],
            stdin=stream, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )


def _exchange(args, scope, payload):
    connected = False
    if not hasattr(socket, "AF_UNIX"):
        _capture(args, payload)
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        try:
            deadline = time.monotonic() + RESPONSE_SECONDS
            _remaining(client, deadline)
            client.connect(os.path.join(args.dest, "memory", "hooks.sock"))
            connected = True
            request = {"agent": args.agent_id, "scope": scope, "event": args.event,
                       "format": args.format, "payload": payload}
            _remaining(client, deadline)
            encoded = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
            if len(encoded) > FRAME_LIMIT:
                raise ValueError("oversized broker request")
            client.sendall(encoded)
            response = b""
            while b"\n" not in response:
                _remaining(client, deadline)
                chunk = client.recv(min(64000, FRAME_LIMIT + 1 - len(response)))
                if not chunk:
                    raise ValueError("incomplete broker frame")
                response += chunk
                if len(response) > FRAME_LIMIT:
                    raise ValueError("oversized broker frame")
            reply = json.loads(response)
            if not isinstance(reply, dict) or not isinstance(reply.get("context", ""), str):
                raise ValueError("invalid broker reply")
            token = reply.get("delivery_token")
            if token is not None and (not isinstance(token, str) or len(token) > 256):
                raise ValueError("invalid receipt token")
            _remaining(client, deadline)
            _emit(args.format, args.event, reply)
            # A failed receipt cannot retract stdout or cause a second response.
            if token and args.format != "windsurf":
                try:
                    deadline = time.monotonic() + RECEIPT_SECONDS
                    _remaining(client, deadline)
                    client.sendall(json.dumps({"operation": "ack", "delivery_token": token}).encode() + b"\n")
                    client.shutdown(socket.SHUT_WR)
                    _remaining(client, deadline)
                    client.recv(4096)  # Wait for broker commit/close, bounded by the receipt deadline.
                except OSError:
                    pass
            return True
        except OSError:
            if not connected:
                _capture(args, payload)
            raise


def main(argv=None):
    # A generated hook is not a terminal UI. Fixed help width also avoids
    # importing shutil/compression modules just to probe terminal dimensions.
    parser = _Parser(description=__doc__, formatter_class=lambda prog: argparse.HelpFormatter(prog, width=80))
    parser.add_argument("--dest", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--scope", default="{}")
    parser.add_argument("--event", required=True, choices=EVENTS)
    parser.add_argument("--format", choices=AGENTS)
    parser.add_argument("--python")  # Compatibility with previously generated Node commands.
    args = None
    empty = {"context": "", "injected": []}
    try:
        args = parser.parse_args(argv)
        raw = sys.stdin.buffer.read(FRAME_LIMIT + 1)
        if len(raw) > FRAME_LIMIT:
            raise ValueError("oversized host input")
        payload, scope = json.loads(raw), json.loads(args.scope)
        if not isinstance(payload, dict) or not isinstance(scope, dict):
            raise ValueError("host payload and scope must be objects")
        if _exchange(args, scope, payload):
            return 0
    except TimeoutError:
        empty["timed_out"] = True
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    try:
        _emit(args.format if args else None, args.event if args else "", empty)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
