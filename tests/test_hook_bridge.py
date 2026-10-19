"""Exercise the installed native transport as a fresh isolated process."""

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pytest

from commontrace.hook_kit import AGENTS, HookKit
from commontrace.memory_journal import Journal
from tests.test_hook_service import functional_command

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket transport")

# Upper bound for starting a bridge process (Node, then Python) under a loaded,
# coverage-instrumented parallel run; a bound, not a latency assertion.
SPAWN_TIMEOUT = 30


def command(root):
    return shlex.split(HookKit(str(root), "agent", embedder="none").manifest("claude")["events"]["UserPromptSubmit"])


def run(root, *, agent=None, payload=None, functional=False, **kwargs):
    base = functional_command(command(root)) if functional else command(root)
    args = base + (["--format", agent] if agent else [])
    return subprocess.run(
        args, input=payload if payload is not None else '{"session_id":"s","prompt":"code"}',
        text=True, capture_output=True, timeout=SPAWN_TIMEOUT, check=True, **kwargs,
    )


@contextmanager
def peer(root, handler):
    (root / "memory").mkdir(exist_ok=True)
    errors = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(root / "memory" / "hooks.sock"))
        server.listen(1)
        server.settimeout(SPAWN_TIMEOUT)

        def work():
            try:
                with server.accept()[0] as client:
                    client.settimeout(5)
                    handler(client)
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            yield
        finally:
            worker.join(timeout=SPAWN_TIMEOUT + 1)
            assert not worker.is_alive(), "transport failed to disconnect"
            if errors:
                raise errors[0]


def frame(client):
    data = b""
    while b"\n" not in data:
        chunk = client.recv(100001)
        if not chunk:
            return data
        data += chunk
    return data


def test_manifest_selects_isolated_installed_interpreter(tmp_path):
    args = command(tmp_path)
    assert args[:3] == [sys.executable, "-I", "-S"]
    assert os.path.isabs(args[3]) and args[3].endswith("hook_bridge.py")
    assert os.path.isfile(args[3])


def test_warm_transport_avoids_argparse_import(tmp_path):
    def handler(client):
        frame(client)
        client.sendall(b'{"context":"","injected":[]}\n')

    args = command(tmp_path)
    args[1:1] = ["-X", "importtime"]
    with peer(tmp_path, handler):
        result = subprocess.run(
            args, input='{"session_id":"s"}', text=True, capture_output=True, timeout=SPAWN_TIMEOUT, check=True
        )
    assert "argparse" not in result.stderr
    assert json.loads(result.stdout) == {"context": "", "injected": []}


def test_help_and_invalid_options_keep_native_hook_fail_safe(tmp_path):
    help_result = subprocess.run(
        command(tmp_path) + ["--help"], input="", text=True, capture_output=True, timeout=SPAWN_TIMEOUT, check=True
    )
    assert "--dest" in help_result.stdout
    assert "--agent-id" in help_result.stdout
    assert "--event" in help_result.stdout
    assert "Admission, normalization, capture" in help_result.stdout
    assert not help_result.stderr

    invalid = subprocess.run(
        command(tmp_path) + ["--not-a-hook-option"],
        input='{"session_id":"s"}', text=True, capture_output=True, timeout=SPAWN_TIMEOUT, check=True,
    )
    assert json.loads(invalid.stdout) == {"context": "", "injected": []}
    assert not invalid.stderr


def test_equals_options_and_legacy_python_flag_preserve_delivery(tmp_path):
    observed = {}

    def handler(client):
        observed["request"] = json.loads(frame(client))
        client.sendall(b'{"context":"delivered","delivery_token":"receipt"}\n')
        observed["receipt"] = json.loads(frame(client))

    generated = command(tmp_path)
    equal_form = generated[:4] + [
        "--dest=" + generated[5],
        "--agent-i=" + generated[7],
        "--scope=" + generated[9],
        "--eve=" + generated[11],
        "--format=claude",
        "--python=/legacy/interpreter",
    ]
    with peer(tmp_path, handler):
        result = subprocess.run(
            equal_form,
            input='{"session_id":"s","prompt":"code"}',
            text=True,
            capture_output=True,
            timeout=SPAWN_TIMEOUT,
            check=True,
        )
    assert json.loads(result.stdout) == {
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "delivered"}
    }
    req_payload = observed["request"].pop("payload", {})
    assert req_payload.get("session_id") == "s" and req_payload.get("prompt") == "code"
    assert len(req_payload.get("event_id", "")) == 32
    assert observed["request"] == {
        "agent": "agent",
        "scope": {"agent": "agent"},
        "event": "UserPromptSubmit",
        "format": "claude",
    }
    assert observed["receipt"] == {"operation": "ack", "delivery_token": "receipt"}
    assert not result.stderr


@pytest.mark.parametrize("invalid_option", ["--event=NotAnEvent", "--format=not-an-agent", "--event"])
def test_invalid_choices_and_missing_values_fail_closed(tmp_path, invalid_option):
    result = subprocess.run(
        command(tmp_path) + [invalid_option],
        input='{"session_id":"s"}',
        text=True,
        capture_output=True,
        timeout=SPAWN_TIMEOUT,
        check=True,
    )
    assert json.loads(result.stdout) == {"context": "", "injected": []}
    assert not result.stderr


@pytest.mark.parametrize("agent", [None, *AGENTS])
def test_all_formats_preserve_protocol_and_receipt(tmp_path, agent):
    offered = {"context": "<memory_update>Keep credentials private. ☃</memory_update>",
               "injected": ["rule"], "delivery_token": "opaque-receipt"}
    observed = {}

    def handler(client):
        observed["request"] = json.loads(frame(client))
        client.sendall(json.dumps(offered).encode() + b"\n")
        observed["receipt"] = frame(client)

    with peer(tmp_path, handler):
        result = run(tmp_path, agent=agent, functional=True)
    context = offered["context"]
    expected = {
        None: {"context": context, "injected": ["rule"]},
        "cline": {"cancel": False, "contextModification": context},
        "openhands": {"decision": "allow", "additionalContext": context},
        "windsurf": {}, "kimi": context, "cursor": {"additional_context": context},
        **{name: {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
           for name in ("claude", "codex", "gemini")},
    }[agent]
    assert (result.stdout.rstrip("\n") if isinstance(expected, str) else json.loads(result.stdout)) == expected
    assert not result.stderr
    assert "opaque-receipt" not in result.stdout
    req_payload = observed["request"].pop("payload", {})
    assert req_payload.get("session_id") == "s" and req_payload.get("prompt") == "code"
    assert len(req_payload.get("event_id", "")) == 32
    assert observed["request"] == {"agent": "agent", "scope": {"agent": "agent"},
                                   "event": "UserPromptSubmit", "format": agent}
    # Windsurf stdout is capture-only UI output, so it cannot attest context delivery.
    assert (json.loads(observed["receipt"]) if observed["receipt"] else None) == (
        None if agent == "windsurf" else {"operation": "ack", "delivery_token": "opaque-receipt"}
    )


@pytest.mark.parametrize("reply", [b"invalid-json\n", b"[]\n", b"null\n", b'{"context":5}\n',
                                   b'{"context":"' + b"x" * 100000 + b'"}\n',
                                   b'{"context":"late","delivery_token":[]}\n',
                                   b'{"nested":' + b"[" * 10000 + b"0" + b"]" * 10000 + b'}\n'],
                         ids=["syntax", "array", "null", "context-type", "oversize", "token-type", "deep"])
def test_malformed_and_oversized_replies_do_not_inject_or_ack(tmp_path, reply):
    observed = []

    def handler(client):
        frame(client)
        try:
            client.sendall(reply)
            observed.append(frame(client))
        except (BrokenPipeError, ConnectionResetError):
            observed.append(b"")

    with peer(tmp_path, handler):
        result = run(tmp_path)
    assert json.loads(result.stdout)["context"] == ""
    assert observed == [b""]
    assert not result.stderr


def test_deadline_covers_slow_frame_and_does_not_ack(tmp_path):
    observed = []

    def handler(client):
        frame(client)
        try:
            # A loaded peer may be scheduled after the client's bounded deadline.
            # Closure before the first partial write is the same fail-open contract.
            client.sendall(b'{"context":"')
            time.sleep(0.08)
            client.sendall(b'late","delivery_token":"receipt"}\n')
            observed.append(frame(client))
        except (BrokenPipeError, ConnectionResetError):
            observed.append(b"")

    with peer(tmp_path, handler):
        result = run(tmp_path)
    assert json.loads(result.stdout)["context"] == ""
    assert json.loads(result.stdout)["timed_out"] is True
    assert observed == [b""]


def test_successful_stdout_precedes_ack_and_stalled_receipt_is_bounded(tmp_path):
    read_output = threading.Event()
    release_peer = threading.Event()
    observed = []
    native = command(tmp_path)
    # Test transport ordering with scheduling headroom, independently of the
    # production 25/20 ms deadlines exercised by deadline tests and latency CI.
    script = """
import importlib.util
import sys
spec = importlib.util.spec_from_file_location("bridge", sys.argv[1])
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
bridge.RESPONSE_SECONDS = 2.0
bridge.RECEIPT_SECONDS = 0.5
raise SystemExit(bridge.main(sys.argv[2:]))
"""
    functional = native[:3] + ["-c", script, *native[3:]]

    def handler(client):
        frame(client)
        client.sendall(b'{"context":"delivered","delivery_token":"receipt"}\n')
        observed.append(json.loads(frame(client)))
        assert read_output.wait(5)
        # Keep the peer open until the client has exited: successful teardown
        # cannot be explained by a timed sleep closing the remote socket.
        assert release_peer.wait(5)

    with peer(tmp_path, handler):
        with subprocess.Popen(functional, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as process:
            try:
                process.stdin.write('{"session_id":"s"}')
                process.stdin.close()
                assert json.loads(process.stdout.readline())["context"] == "delivered"
                read_output.set()
                assert process.wait(timeout=SPAWN_TIMEOUT) == 0
                assert process.stderr.read() == ""
            finally:
                release_peer.set()
    assert observed == [{"operation": "ack", "delivery_token": "receipt"}]


def test_broken_stdout_never_acknowledges_delivery(tmp_path):
    observed = []

    def handler(client):
        frame(client)
        try:
            client.sendall(b'{"context":"undelivered","delivery_token":"receipt"}\n')
            observed.append(frame(client))
        except (BrokenPipeError, ConnectionResetError):
            # The bridge answers within a 25 ms budget. Under a loaded parallel
            # run it can give up and close (or reset) before this exchange
            # finishes; it then acknowledged nothing, the property under test.
            observed.append(b"")

    with peer(tmp_path, handler):
        with subprocess.Popen(command(tmp_path), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as process:
            process.stdout.close()
            process.stdin.write('{"session_id":"s"}')
            process.stdin.close()
            assert process.wait(timeout=SPAWN_TIMEOUT) == 0
            assert process.stderr.read() == ""
    assert observed == [b""]


@pytest.mark.parametrize("payload", ["null", "[]", "not-json", '{"prompt":"' + "x" * 100000 + '"}',
                                     '{"nested":' + "[" * 10000 + "0" + "]" * 10000 + '}'],
                         ids=["null", "array", "syntax", "oversize", "deep"])
def test_invalid_input_fails_closed_without_creating_capture(tmp_path, payload):
    result = run(tmp_path, payload=payload)
    assert json.loads(result.stdout)["context"] == ""
    assert not result.stderr
    with Journal(str(tmp_path)).connect() as db:
        assert db.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 0


def test_unrelated_directory_and_pythonpath_cannot_supply_transport_imports(tmp_path, tmp_path_factory):
    unrelated = tmp_path / "unrelated directory"
    unrelated.mkdir()
    for name in ("json.py", "socket.py", "argparse.py", "sitecustomize.py"):
        (unrelated / name).write_text('raise RuntimeError("foreign import")\n')
    # Keep the Unix socket path below AF_UNIX's platform limit while retaining
    # spaces in the store path. The test's long name makes tmp_path too deep.
    root = tmp_path_factory.mktemp("store with spaces")

    def handler(client):
        frame(client)
        client.sendall(b'{"context":"isolated","injected":[]}\n')

    with peer(root, handler):
        # This asserts isolated imports, with the same -I -S interpreter flags.
        # Scheduling deadlines are exercised by the dedicated timeout tests.
        result = subprocess.run(
            functional_command(command(root)), input='{"session_id":"s","prompt":"code"}',
            text=True, capture_output=True, timeout=SPAWN_TIMEOUT, check=True,
            cwd=unrelated, env={**os.environ, "PYTHONPATH": str(unrelated)},
        )
    assert json.loads(result.stdout)["context"] == "isolated"
    assert not result.stderr


def test_missing_broker_detaches_capture_from_unrelated_directory(tmp_path):
    root = tmp_path / "store with spaces"
    unrelated = tmp_path / "working directory"
    root.mkdir()
    unrelated.mkdir()
    (unrelated / "sitecustomize.py").write_text('raise RuntimeError("foreign startup")\n')
    result = run(root, agent="cline", payload=json.dumps({"taskId": "recovery", "prompt": "capture this"}),
                 cwd=unrelated, env={**os.environ, "PYTHONPATH": str(unrelated)})
    assert json.loads(result.stdout) == {"cancel": False, "contextModification": ""}
    assert not result.stderr
    journal = Journal(str(root))
    for _ in range(200):
        with journal.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
            exposure = db.execute("SELECT COUNT(*) FROM injected").fetchone()[0]
        if count:
            break
        time.sleep(0.01)
    assert count == 1
    assert exposure == 0


@pytest.mark.skipif(not shutil.which("node"), reason="legacy Node command compatibility")
def test_previously_installed_node_command_delegates_to_same_transport(tmp_path):
    bridge = os.path.join(os.path.dirname(command(tmp_path)[3]), "ui", "hook-bridge.mjs")

    def handler(client):
        request = json.loads(frame(client))
        assert request["format"] == "cursor"
        client.sendall(b'{"context":"legacy command","delivery_token":"receipt"}\n')
        assert json.loads(frame(client)) == {"operation": "ack", "delivery_token": "receipt"}

    with peer(tmp_path, handler):
        result = subprocess.run([shutil.which("node"), bridge, "--python", sys.executable,
                                 "--dest", str(tmp_path), "--agent-id", "agent", "--scope", '{}',
                                 "--event", "UserPromptSubmit", "--format", "cursor"],
                                input='{"session_id":"s"}', text=True, capture_output=True,
                                timeout=SPAWN_TIMEOUT, check=True)
    assert json.loads(result.stdout) == {"additional_context": "legacy command"}
    assert not result.stderr
