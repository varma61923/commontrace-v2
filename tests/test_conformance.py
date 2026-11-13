import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from unimem import conformance, gateway, holdout_io, paths
from unimem.cli import main

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFERENCE = f"{sys.executable} -m unimem.conformance"


def test_the_committed_vectors_are_exactly_what_the_reference_produces():
    assert all(r.ok for r in conformance.check_vectors_against_reference(conformance.load_vectors()))


def test_the_packaged_vectors_and_the_copy_beside_the_spec_are_identical():
    assert Path(conformance.VECTORS_PATH).read_text(encoding="utf-8") == Path(conformance.SPEC_VECTORS_PATH).read_text(
        encoding="utf-8"
    )


def test_the_vectors_are_deterministic_and_cover_the_edges():
    a, b = conformance.build_vectors(), conformance.build_vectors()
    assert a == b
    rates = {v["rate"] for v in a["assign"]}
    assert (
        {0.0, 1.0} <= rates and any(not v["held_out"] for v in a["assign"]) and any(v["held_out"] for v in a["assign"])
    )
    assert any(not v["lesson"].isascii() for v in a["assign"]) and any("\x1f" in v["lesson"] for v in a["assign"])
    assert [len(v["rows"]) for v in a["ledger"]] == [0, 1, 3, 6]


def test_the_reference_passes_over_stdio():
    results = conformance.run_exec(REFERENCE, conformance.load_vectors())
    assert [r.name for r in results] == [
        "vectors:assign",
        "vectors:ledger",
        "vectors:digest",
        "vectors:revision",
        "vectors:allocate",
        "vectors:learning",
    ]
    assert all(r.ok for r in results), [r.detail for r in results if not r.ok]


def test_a_program_that_orders_the_hash_input_differently_is_caught(tmp_path):
    script = tmp_path / "wrong.py"
    script.write_text(
        "import sys, json, hashlib\n"
        "for line in sys.stdin:\n"
        "    r = json.loads(line)\n"
        "    if r['op'] != 'assign':\n        print(json.dumps({'error': 'x'})); continue\n"
        "    rate = r['rate']\n"
        "    if rate <= 0: held = False\n"
        "    elif rate >= 1: held = True\n"
        "    else:\n"
        "        d = hashlib.blake2b('\\x1f'.join([r['lesson'], r['occasion'], r['salt']]).encode(), digest_size=8).digest()\n"
        "        held = int.from_bytes(d, 'big') / 2**64 < rate\n"
        "    print(json.dumps({'held_out': held}), flush=True)\n"
    )
    results = conformance.run_exec(f"{sys.executable} {script}", conformance.load_vectors(), only=("assign",))
    assert not results[0].ok and "first miss" in results[0].detail


def test_a_short_answer_a_crash_and_silence_are_reported_not_hung(tmp_path):
    vectors = conformance.load_vectors()
    assert "0 answers" in conformance.run_exec("true", vectors)[0].detail
    crash = tmp_path / "crash.py"
    crash.write_text("import sys\nsys.stderr.write('boom')\nsys.exit(3)\n")
    assert "boom" in conformance.run_exec(f"{sys.executable} {crash}", vectors)[0].detail
    assert "no answer within" in conformance.run_exec("sleep 5", vectors, timeout=0.5)[0].detail


def test_a_subset_is_reported_as_a_subset(capsys):
    assert main(["conformance", "exec", REFERENCE, "--only", "assign,digest"]) == 0
    out = capsys.readouterr().out
    assert "vectors:assign" in out and "vectors:digest" in out and "vectors:ledger" not in out
    assert main(["conformance", "exec", REFERENCE, "--only", "nope"]) == 2


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "support", "--dest", str(tmp_path)])
    holdout_io.configure(str(tmp_path), rate=0.5, salt="conf-1")
    return str(tmp_path)


def test_a_store_whose_assignments_follow_the_randomization_passes(store):
    for i in range(60):
        holdout_io.assign_and_log(
            store, ["m1", "m2"], occasion_id=f"o{i}", rate=0.5, salt="conf-1", revisions={"m1": "r", "m2": "r"}
        )
    results = conformance.check_store(store)
    assert all(r.ok for r in results), [(r.name, r.detail) for r in results if not r.ok]


def test_a_store_that_flipped_assignments_is_not_randomizing(store):
    for i in range(30):
        holdout_io.assign_and_log(store, ["m1"], occasion_id=f"o{i}", rate=0.5, salt="conf-1", revisions={"m1": "r"})
    path = holdout_io.holdout_log_path(store)
    rows = [json.loads(line) for line in Path(path).read_text().splitlines(keepends=True)]
    for row in rows[:5]:
        row["injected"] = not row["injected"]
    Path(path).write_text("".join((json.dumps(r) + "\n" for r in rows)))
    failed = [r for r in conformance.check_store(store) if not r.ok]
    assert [r.name for r in failed] == ["store:assignments follow the randomization"]


def test_a_malformed_lesson_fails_the_store_check(store):
    bad = os.path.join(paths.lessons_dir(store), "lesson_bad.md")
    Path(bad).write_text("---\nname: bad\nstatus: not-a-status\n---\nbody\n")
    failed = [r.name for r in conformance.check_store(store) if not r.ok]
    assert "store:lessons validate" in failed


@pytest.fixture
def running(store):
    g = gateway.Gateway(store, token="t" * 40)
    server = gateway.make_http_server(g, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_our_gateway_passes_the_gateway_checks(running):
    results = conformance.check_gateway(running, "t" * 40)
    assert len(results) >= 12 and all(r.ok for r in results), [(r.name, r.detail) for r in results if not r.ok]


def test_a_dead_gateway_fails_cleanly(capsys):
    results = conformance.check_gateway("http://127.0.0.1:9", "x")
    assert len(results) == 1 and not results[0].ok
    assert main(["conformance", "gateway", "http://127.0.0.1:9", "--token", "x"]) == 1


def test_the_report_names_what_it_does_not_test():
    text = conformance.render([conformance.Result("a", True)])
    assert "1/1 checks passed" in text and "not tested" in text and "statistics" in text


def test_missing_vectors_cannot_claim_conformance():
    vectors = conformance.load_vectors()
    vectors["learning"] = []
    results = conformance.run_exec(REFERENCE, vectors, only=("learning",))
    assert len(results) == 1 and not results[0].ok
    assert "no vectors" in results[0].detail


def test_correct_answers_followed_by_a_crash_fail_conformance(tmp_path):
    script = tmp_path / "crash_after_answers.py"
    script.write_text("from unimem.conformance import main_reference\nmain_reference()\nraise SystemExit(3)\n")
    results = conformance.run_exec(f"{sys.executable} {script}", conformance.load_vectors())
    assert len(results) == 1 and not results[0].ok
    assert "status 3" in results[0].detail


@pytest.mark.parametrize("profiles", [("embedded",), ("embedded", "http", "robot")])
def test_learning_conformance_replays_real_acknowledged_evidence(profiles):
    report = conformance.check_learning_lifecycle(profiles=profiles, samples=3)
    assert report["passed"], [row for row in report["checks"] if not row["ok"]]
    assert report["expected_refusals"] == 4 * len(profiles)
    assert report["store_bytes_before_cleanup"] > 0
    assert {row["adapter_operation"].partition(":")[0] for row in report["measurements"]} == set(profiles)
    for row in report["measurements"]:
        assert 0 <= row["p50_ms"] <= row["p95_ms"] <= row["p99_ms"]


def test_learning_conformance_mcp_shares_the_authenticated_scope():
    pytest.importorskip("mcp")
    report = conformance.check_learning_lifecycle(profiles=("embedded", "http", "mcp", "robot"), samples=3)
    assert report["passed"], [row for row in report["checks"] if not row["ok"]]


def test_learning_conformance_fails_when_an_adapter_loses_evidence(monkeypatch):
    from unimem.client import LearningClient

    original = LearningClient.call

    def incomplete(self, operation, payload=None):
        result = original(self, operation, payload)
        return {**result, "occasions": 0} if operation == "status" else result

    monkeypatch.setattr(LearningClient, "call", incomplete)
    report = conformance.check_learning_lifecycle(profiles=("embedded",), samples=1)
    assert not report["passed"]
    assert any(not row["ok"] and "occasion-count" in row["name"] for row in report["checks"])


@pytest.mark.parametrize("args", [["--samples", "0"], ["--profiles", "unknown"], ["--profiles", "embedded,embedded"]])
def test_learning_conformance_cli_rejects_invalid_workloads(args):
    assert main(["conformance", "learning", *args]) == 2


@pytest.mark.skipif(
    not os.environ.get("UNIMEM_CONFORMANCE_GO") or shutil.which("go") is None,
    reason="set UNIMEM_CONFORMANCE_GO=1 with Go installed (downloads golang.org/x/crypto)",
)
def test_an_independent_go_implementation_written_from_the_spec_passes_three_layers(tmp_path):
    src = os.path.join(ROOT, "protocol", "conformance", "examples", "go")
    binary = tmp_path / "conf"
    subprocess.run(
        ["go", "build", "-o", str(binary), "."], cwd=src, check=True, env={**os.environ, "GOFLAGS": "-mod=mod"}
    )
    results = conformance.run_exec(str(binary), conformance.load_vectors(), only=("assign", "ledger", "digest"))
    assert all(r.ok for r in results), [r.detail for r in results if not r.ok]


def test_the_runner_never_hands_the_command_to_a_shell(tmp_path):
    marker = tmp_path / "pwned"
    results = conformance.run_exec(f"true; touch {marker}", conformance.load_vectors(), only=("digest",))
    assert not marker.exists() and not results[0].ok


def test_a_gateway_url_that_is_not_http_is_refused():
    results = conformance.check_gateway("file:///etc/passwd", "x")
    assert len(results) == 1 and not results[0].ok


def test_gateway_redirect_does_not_forward_bearer_token():
    received = []

    class Sink(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    sink = HTTPServer(("127.0.0.1", 0), Sink)

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/")
            self.end_headers()

        def log_message(self, *args):
            pass

    source = HTTPServer(("127.0.0.1", 0), Redirect)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (source, sink)]
    for thread in threads:
        thread.start()
    try:
        status, _ = conformance._call(f"http://127.0.0.1:{source.server_port}", "GET", "/", "test-token")
        assert status == 302
        assert received == []
    finally:
        for server in (source, sink):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


@pytest.mark.parametrize(
    "body",
    [b"not-json", b"[]", b'"string"', b"\xff", b'{"data":"' + b"x" * (8 * 1024 * 1024) + b'"}'],
    ids=["invalid-json", "array", "string", "invalid-encoding", "oversized"],
)
def test_gateway_invalid_json_response_is_reported(body):
    class InvalidResponse(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), InvalidResponse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        results = conformance.check_gateway(f"http://127.0.0.1:{server.server_port}", "token")
        assert len(results) == 1 and not results[0].ok
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("response", [{"deliver": None}, {"deliver": [{}]}, {"withheld": [[]]}])
def test_gateway_malformed_recall_response_is_reported(monkeypatch, response):
    def call(base, method, path, token, body=None):
        if path == "/v1/status":
            return 401, {}
        if path == "/v1/recall":
            return 200, response
        return 200, {}

    monkeypatch.setattr(conformance, "_call", call)
    results = conformance.check_gateway("http://example.test", "token")
    assert any(r.name == "gateway:recall partitions the candidates" and not r.ok for r in results)


def test_gateway_repeated_recall_checks_withheld_partition(monkeypatch):
    recalls = 0

    def call(base, method, path, token, body=None):
        nonlocal recalls
        if path == "/v1/status":
            return 401, {}
        if path == "/v1/recall" and body and isinstance(body.get("items"), list):
            recalls += 1
            return 200, {"deliver": body["items"], "withheld": [] if recalls == 1 else ["extra"]}
        return 200, {}

    monkeypatch.setattr(conformance, "_call", call)
    results = conformance.check_gateway("http://example.test", "token")
    assert any(r.name == "gateway:the same occasion gets the same partition" and not r.ok for r in results)
