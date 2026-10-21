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

from unimem.hook_kit import HookKit
from unimem.memory_journal import Journal
from unimem.memory_record import MemoryRecord


def functional_command(command):
    # Protocol checks allow shared-runner scheduling headroom. The native
    # latency tests continue to launch the bridge with production deadlines.
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
    return command[:3] + ["-c", script, *command[3:]]


def test_native_transport_capture_receipt_and_crash_recovery(tmp_path):
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("native Unix transport requires Unix sockets")
    root = str(tmp_path)
    (tmp_path / "unimem.yaml").write_text("profiles:\n  local:\n    embedding: none\n")
    journal = Journal(root)
    journal.put(
        MemoryRecord("critical", "directive", 1, {"text": "Keep credentials private."}, scope={"agent": "agent"})
    )
    broker_script = """
import sys
from functools import partialmethod
from unimem.hook_service import HookKit, main
HookKit.handle = partialmethod(HookKit.handle, deadline_ms=1000)
raise SystemExit(main(sys.argv[1:]))
"""
    broker = subprocess.Popen(
        [sys.executable, "-c", broker_script, "--dest", root],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ready_by = time.monotonic() + 15
        while not os.path.exists(tmp_path / "memory" / "hooks.sock"):
            assert broker.poll() is None
            if time.monotonic() >= ready_by:
                pytest.fail("native broker did not become ready within the startup budget")
            time.sleep(0.01)
        native = shlex.split(HookKit(root, "agent").manifest("claude")["events"]["UserPromptSubmit"])
        command = functional_command(native)
        first = subprocess.run(
            command + ["--format", "claude"],
            input=json.dumps({"session_id": "s", "prompt": "code"}),
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        output = json.loads(first.stdout)
        assert "critical" in output["hookSpecificOutput"]["additionalContext"]
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
                try:
                    client.sendall(reply)
                    # Deliberately leave the response side open after an ACK.
                    while client.recv(100000):
                        pass
                except (BrokenPipeError, ConnectionResetError):
                    # Under load the client's bounded deadline may close its
                    # socket before this peer gets CPU to send the reply. That
                    # is also an observed disconnect, rather than a live peer.
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
        result = subprocess.run(
            functional_command(command) + ["--format", "claude"],
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


def test_broker_socket_is_never_created_wider_than_owner_only(tmp_path, monkeypatch):
    """bind() runs under a restrictive umask, so a permissive process umask cannot expose the endpoint."""
    import os
    import socket

    from unimem import hook_service

    seen = []
    real_bind = socket.socket.bind

    def recording_bind(self, address):
        current = os.umask(0o177)
        os.umask(current)
        seen.append(current)
        raise KeyboardInterrupt  # stop serve() right after the bind attempt

    monkeypatch.setattr(socket.socket, "bind", recording_bind)
    previous = os.umask(0o000)  # the hostile case: a fully permissive process umask
    try:
        try:
            hook_service.serve(str(tmp_path))
        except KeyboardInterrupt:
            pass
    finally:
        os.umask(previous)
        monkeypatch.setattr(socket.socket, "bind", real_bind)
    assert seen == [0o177]


def test_a_running_broker_bridges_a_lesson_approved_after_it_started(tmp_path):
    """No re-run of `up` and no broker restart: approving a lesson is enough for it to be injected."""
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("native Unix transport requires Unix sockets")
    from unimem import frontmatter
    from unimem.bootstrap_runtime import setup
    from unimem.cli import main as cli

    root = str(tmp_path)
    (tmp_path / "unimem.yaml").write_text("profiles:\n  local:\n    embedding: none\n")
    repo = hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:24]
    agent_id = "claude-" + repo
    setup(root, config={"agents": {"claude": {"config_path": "hooks.json"}}, "histories": []})
    broker = subprocess.Popen(
        [sys.executable, "-c", "import sys\nfrom unimem.hook_service import main\nraise SystemExit(main(sys.argv[1:]))",
         "--dest", root], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ready_by = time.monotonic() + 15
        while not os.path.exists(tmp_path / "memory" / "hooks.sock"):
            assert broker.poll() is None
            if time.monotonic() >= ready_by:
                pytest.fail("native broker did not become ready within the startup budget")
            time.sleep(0.01)
        # A directive injects without embeddings, so this isolates the bridge from retrieval quality.
        assert cli(["lesson", "new", "--slug", "never-print-secrets", "--domain", "security", "--importance", "5",
                    "--description", "Never print credentials", "--importance-rationale", "leak prevention",
                    "--applies-when", "any command touches credentials", "--dest", root]) == 0
        path = tmp_path / "memory" / "lessons" / "lesson_never-print-secrets.md"
        fm, _ = frontmatter.read(str(path))
        fm["lesson_kind"] = "directive"
        frontmatter.write(str(path), fm, "## Rule\nNever print credentials to the terminal.\n\n## Why\nLeaks.\n\n"
                          "## How to apply\nRedact before output.\n")
        assert cli(["lesson", "approve", "never-print-secrets", "--force", "--dest", root]) == 0
        native = shlex.split(HookKit(root, agent_id, scope={"repo": repo}).manifest("claude")["events"]["UserPromptSubmit"])
        command = functional_command(native) + ["--format", "claude"]
        deadline, context = time.monotonic() + 20, ""
        while time.monotonic() < deadline and "credentials" not in context:
            time.sleep(0.5)
            reply = subprocess.run(command, input=json.dumps({"session_id": f"s-{time.monotonic()}", "prompt": "hello"}),
                                   text=True, capture_output=True, timeout=10, check=True)
            context = json.loads(reply.stdout).get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "Never print credentials to the terminal." in context
    finally:
        broker.terminate()
        broker.wait(timeout=10)
