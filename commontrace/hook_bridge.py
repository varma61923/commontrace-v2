"""Stdlib-only native transport, also launched directly with Python -I -S.

Admission, normalization, capture and memory selection belong to HookKit's
broker. This client only frames requests, formats replies and confirms delivery.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import uuid

EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "PreCompact", "SubagentStart", "SubagentStop", "Stop", "SessionEnd",
)
AGENTS = ("claude", "codex", "cursor", "cline", "gemini", "kimi", "windsurf", "openhands")
FRAME_LIMIT = 100000
RESPONSE_SECONDS = 0.050
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


class _Arguments:
    __slots__ = ("dest", "agent_id", "scope", "event", "format", "python")

    def __init__(self, values):
        for name in self.__slots__:
            setattr(self, name, values[name])


_HELP = object()
_OPTIONS = {
    "--dest": "dest",
    "--agent-id": "agent_id",
    "--scope": "scope",
    "--event": "event",
    "--format": "format",
    "--python": "python",  # Compatibility with previously generated Node commands.
}


def _parse_args(argv):
    """Parse the fixed native hook interface without importing argparse at startup."""
    tokens = sys.argv[1:] if argv is None else argv
    values = {"dest": None, "agent_id": None, "scope": "{}", "event": None, "format": None, "python": None}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("-h", "--help"):
            return _HELP
        option, separator, value = token.partition("=")
        name = _OPTIONS.get(option)
        if name is None and option.startswith("--"):
            matches = [candidate for candidate in _OPTIONS if candidate.startswith(option)]
            if len(matches) == 1:
                name = _OPTIONS[matches[0]]
        if name is None:
            raise ValueError("unrecognized native hook option")
        if not separator:
            index += 1
            if index == len(tokens) or tokens[index].startswith("-"):
                raise ValueError("native hook option requires a value")
            value = tokens[index]
        values[name] = value
        index += 1

    if values["dest"] is None or values["agent_id"] is None or values["event"] is None:
        raise ValueError("missing required native hook option")
    if values["event"] not in EVENTS:
        raise ValueError("unsupported native hook event")
    if values["format"] is not None and values["format"] not in AGENTS:
        raise ValueError("unsupported native hook format")
    return _Arguments(values)


def _print_help():
    events = "{" + ",".join(EVENTS) + "}"
    agents = "{" + ",".join(AGENTS) + "}"
    prog = os.path.basename(sys.argv[0])
    prefix = "usage: " + prog + " "
    lines = [
        prefix + "[-h] --dest DEST --agent-id AGENT_ID [--scope SCOPE]",
        " " * len(prefix) + f"--event {events} [--format {agents}] [--python PYTHON]",
        "",
        *__doc__.splitlines(),
        "",
        "options:",
        "  -h, --help           show this help message and exit",
        "  --dest DEST",
        "  --agent-id AGENT_ID",
        "  --scope SCOPE",
        f"  --event {events}",
        f"  --format {agents}",
        "  --python PYTHON      compatibility with previously generated Node commands",
        "",
    ]
    sys.stdout.write("\n".join(lines))
    sys.stdout.flush()


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

    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bootstrap = (
        "import sys;sys.path.insert(0," + repr(package_root) + ");"
        "from commontrace.hook_kit import main;raise SystemExit(main())"
    )
    with tempfile.TemporaryFile() as stream:
        stream.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        stream.seek(0)
        subprocess.Popen(  # noqa: S603 - interpreter and argv are generated, never a shell
            [args.python or sys.executable, "-I", "-c", bootstrap, "--capture-only",
             "--dest", args.dest, "--agent-id", args.agent_id, "--scope", args.scope,
             "--event", args.event, *(["--format", args.format] if args.format else [])],
            stdin=stream, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )


def _exchange(args, scope, payload):
    if not hasattr(socket, "AF_UNIX"):
        _capture(args, payload)
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        try:
            deadline = time.monotonic() + RESPONSE_SECONDS
            _remaining(client, deadline)
            client.connect(os.path.join(args.dest, "memory", "hooks.sock"))
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
        except (OSError, ValueError, TypeError, RecursionError):
            # A broker can accept a frame and then disappear before its durable
            # capture or reply. Retry capture through the detached writer for any
            # failed exchange. The shared event_id makes this safe when the broker
            # committed the capture just before the response failed.
            _capture(args, payload)
            raise


def main(argv=None):
    args = None
    empty = {"context": "", "injected": []}
    try:
        args = _parse_args(argv)
        if args is _HELP:
            _print_help()
            return 0
        raw = sys.stdin.buffer.read(FRAME_LIMIT + 1)
        if len(raw) > FRAME_LIMIT:
            raise ValueError("oversized host input")
        payload, scope = json.loads(raw), json.loads(args.scope)
        if not isinstance(payload, dict) or not isinstance(scope, dict):
            raise ValueError("host payload and scope must be objects")
        payload = dict(payload)
        event_id = payload.get("event_id")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 256:
            payload["event_id"] = uuid.uuid4().hex
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
