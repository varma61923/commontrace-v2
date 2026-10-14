import hashlib
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time

import pytest

from commontrace.hook_kit import HookKit
from commontrace.memory_journal import Journal
from commontrace.memory_record import MemoryRecord


def test_native_transport_capture_receipt_and_crash_recovery(tmp_path):
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("native Unix transport requires Unix sockets")
    root = str(tmp_path)
    (tmp_path / "commontrace.yaml").write_text("profiles:\n  local:\n    embedding: none\n")
    journal = Journal(root)
    journal.put(
        MemoryRecord("critical", "directive", 1, {"text": "Keep credentials private."}, scope={"agent": "agent"})
    )
    broker = subprocess.Popen(
        [sys.executable, "-m", "commontrace.hook_service", "--dest", root],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if os.path.exists(tmp_path / "memory" / "hooks.sock"):
                break
            assert broker.poll() is None
            time.sleep(0.01)
        command = shlex.split(HookKit(root, "agent").manifest("claude")["events"]["UserPromptSubmit"])
        # A cold/loaded broker may fail open at the native deadline. That must
        # preserve capture without claiming an exposure the model never received.
        for _ in range(10):
            first = subprocess.run(
                command + ["--format", "claude"],
                input=json.dumps({"session_id": "s", "prompt": "code"}),
                text=True,
                capture_output=True,
                # This bounds process startup on shared CI runners. The bridge
                # independently keeps its 25 ms socket deadline; the measured
                # fresh-process benchmark still enforces the latency target.
                timeout=15,
                check=True,
            )
            output = json.loads(first.stdout)
            if "hookSpecificOutput" in output:
                assert "critical" in output["hookSpecificOutput"]["additionalContext"]
                break
            assert output.get("context", "") == ""
            with journal.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM injected WHERE id='critical'").fetchone()[0] == 0
            time.sleep(0.01)
        else:
            pytest.fail("warm broker did not deliver the critical directive")
        second = subprocess.run(
            command + ["--format", "claude"],
            input=json.dumps({"session_id": "s", "prompt": "code"}),
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
        assert "hookSpecificOutput" not in json.loads(second.stdout)
        assert stat_private(tmp_path / "memory" / "hooks.sock")
        with journal.connect() as db:
            before_fallback = db.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
            before_exposures = db.execute("SELECT COUNT(*) FROM injected").fetchone()[0]
        ui_only = subprocess.run(
            command + ["--format", "windsurf"], input=json.dumps({"trajectory_id": "capture-only"}),
            text=True, capture_output=True, timeout=5, check=True,
        )
        assert json.loads(ui_only.stdout) == {}
        ui_run = hashlib.sha256(json.dumps([{"agent": "agent"}, "capture-only"], sort_keys=True).encode()).hexdigest()
        until = time.monotonic() + 5
        while True:
            with journal.connect() as db:
                captured = db.execute("SELECT 1 FROM captures WHERE session=? LIMIT 1", (ui_run,)).fetchone()
            if captured:
                break
            if time.monotonic() >= until:
                pytest.fail("capture-only event was not persisted by the broker or detached fallback")
            time.sleep(0.01)
        with journal.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM injected").fetchone()[0] == before_exposures
            assert db.execute("SELECT COUNT(*) FROM captures").fetchone()[0] > before_fallback
            before_fallback = db.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
    finally:
        broker.kill()
        broker.wait(timeout=5)
    # A stopped broker leaves a stale socket. The bridge succeeds and its detached
    # writer persists capture without attempting inference or fabricating outcome.
    result = subprocess.run(
        command + ["--format", "claude"],
        input=json.dumps({"session_id": "fallback", "prompt": "code"}),
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert json.loads(result.stdout)["context"] == ""
    for _ in range(100):
        with journal.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
        if count > before_fallback:
            break
        time.sleep(0.01)
    assert count > before_fallback


def stat_private(path):
    return os.stat(path).st_mode & 0o777 == 0o600


@pytest.mark.parametrize("reply", [b"invalid-json\n", b'{"context":"","delivery_token":"receipt"}\n'])
def test_native_bridge_closes_malformed_or_stalled_receipt_peer(tmp_path, reply):
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("native Unix transport requires Unix sockets")
    (tmp_path / "memory").mkdir()
    disconnected = threading.Event()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(tmp_path / "memory" / "hooks.sock"))
        server.listen(1)
        server.settimeout(5)

        def peer():
            with server.accept()[0] as client:
                client.settimeout(5)
                client.recv(100000)
                client.sendall(reply)
                # Deliberately leave the response side open after an ACK.
                while client.recv(100000):
                    pass
                disconnected.set()

        worker = threading.Thread(target=peer)
        worker.start()
        command = shlex.split(HookKit(str(tmp_path), "agent").manifest("claude")["events"]["UserPromptSubmit"])
        result = subprocess.run(command, input='{"session_id":"s","prompt":"code"}',
                                text=True, capture_output=True, timeout=15, check=True)
        assert json.loads(result.stdout)["context"] == ""
        worker.join(timeout=5)
        assert disconnected.is_set()


@pytest.mark.parametrize("failure_mode", ["malformed", "timeout"])
@pytest.mark.parametrize("capture_before_failure", [False, True])
def test_native_bridge_recovers_capture_after_accepted_broker_failure(tmp_path, failure_mode, capture_before_failure):
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("native Unix transport requires Unix sockets")
    root = str(tmp_path)
    kit = HookKit(root, "agent")
    journal = kit.journal
    run = hashlib.sha256(json.dumps([kit.scope, "accepted"], sort_keys=True).encode()).hexdigest()
    endpoint = tmp_path / "memory" / "hooks.sock"
    endpoint.parent.mkdir(parents=True, exist_ok=True)
    received = threading.Event()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(endpoint))
        server.listen(1)
        server.settimeout(5)

        def peer():
            with server.accept()[0] as client:
                client.settimeout(5)
                frame = b""
                while b"\n" not in frame:
                    chunk = client.recv(4096)
                    if not chunk:
                        return
                    frame += chunk
                request = json.loads(frame.split(b"\n", 1)[0])
                received.set()
                if capture_before_failure:
                    journal.capture(
                        run,
                        request["event"],
                        request["payload"],
                        scope={**request["scope"], "agent": request["agent"]},
                        event_id=request["payload"]["event_id"],
                    )
                if failure_mode == "malformed":
                    client.sendall(b"not-json\n")
                else:
                    # Keep this
                    # accepted request open until the client times out.
                    while client.recv(4096):
                        pass

        worker = threading.Thread(target=peer)
        worker.start()
        command = shlex.split(kit.manifest("claude")["events"]["UserPromptSubmit"])
        # Recovery after acceptance is a protocol contract. Separate latency
        # tests use the production deadlines; allow scheduling headroom here.
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
        functional = command[:3] + ["-c", script, *command[3:]]
        result = subprocess.run(
            functional + ["--format", "claude"],
            input='{"session_id":"accepted","prompt":"code"}',
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        assert json.loads(result.stdout)["context"] == ""
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert received.is_set()

    for _ in range(100):
        captures = journal.buffer(run, scope=kit.scope)
        if captures:
            break
        time.sleep(0.02)
    assert len(captures) == 1
    assert captures[0]["event"] == "UserPromptSubmit"
    assert captures[0]["payload"]["event_id"]
