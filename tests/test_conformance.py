"""The conformance suite: the committed vectors are the reference's, the runner catches a wrong implementation, the
store and gateway checks pass on ours and fail on broken ones."""
import json
import os
import shutil
import subprocess
import sys
import threading

import pytest

from commontrace import conformance, gateway, holdout_io, paths
from commontrace.cli import main

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFERENCE = f"{sys.executable} -m commontrace.conformance"


def test_the_committed_vectors_are_exactly_what_the_reference_produces():
    assert all(r.ok for r in conformance.check_vectors_against_reference(conformance.load_vectors()))


def test_the_packaged_vectors_and_the_copy_beside_the_spec_are_identical():
    assert open(conformance.VECTORS_PATH, encoding="utf-8").read() == open(conformance.SPEC_VECTORS_PATH,
                                                                           encoding="utf-8").read()


def test_the_vectors_are_deterministic_and_cover_the_edges():
    a, b = conformance.build_vectors(), conformance.build_vectors()
    assert a == b
    rates = {v["rate"] for v in a["assign"]}
    assert {0.0, 1.0} <= rates and any(not v["held_out"] for v in a["assign"]) and any(v["held_out"] for v in a["assign"])
    assert any(not v["lesson"].isascii() for v in a["assign"]) and any("\x1f" in v["lesson"] for v in a["assign"])
    assert [len(v["rows"]) for v in a["ledger"]] == [0, 1, 3, 6]


def test_the_reference_passes_over_stdio():
    results = conformance.run_exec(REFERENCE, conformance.load_vectors())
    assert [r.name for r in results] == ["vectors:assign", "vectors:ledger", "vectors:digest", "vectors:revision"]
    assert all(r.ok for r in results), [r.detail for r in results if not r.ok]


def test_a_program_that_orders_the_hash_input_differently_is_caught(tmp_path):
    """The mistake an implementer reading only the code makes: lesson, occasion, salt instead of salt, lesson, occasion."""
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
        "    print(json.dumps({'held_out': held}), flush=True)\n")
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
        holdout_io.assign_and_log(store, ["m1", "m2"], occasion_id=f"o{i}", rate=0.5, salt="conf-1",
                                  revisions={"m1": "r", "m2": "r"})
    results = conformance.check_store(store)
    assert all(r.ok for r in results), [(r.name, r.detail) for r in results if not r.ok]


def test_a_store_that_flipped_assignments_is_not_randomizing(store):
    for i in range(30):
        holdout_io.assign_and_log(store, ["m1"], occasion_id=f"o{i}", rate=0.5, salt="conf-1", revisions={"m1": "r"})
    path = holdout_io.holdout_log_path(store)
    rows = [json.loads(line) for line in open(path)]
    for row in rows[:5]:
        row["injected"] = not row["injected"]
    open(path, "w").write("".join(json.dumps(r) + "\n" for r in rows))
    failed = [r for r in conformance.check_store(store) if not r.ok]
    assert [r.name for r in failed] == ["store:assignments follow the randomization"]


def test_a_malformed_lesson_fails_the_store_check(store):
    bad = os.path.join(paths.lessons_dir(store), "lesson_bad.md")
    open(bad, "w").write("---\nname: bad\nstatus: not-a-status\n---\nbody\n")
    failed = [r.name for r in conformance.check_store(store) if not r.ok]
    assert "store:lessons validate" in failed


@pytest.fixture
def running(store):
    g = gateway.Gateway(store, token="t" * 40)
    server = gateway.make_http_server(g, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


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


@pytest.mark.skipif(not os.environ.get("COMMONTRACE_CONFORMANCE_GO") or shutil.which("go") is None,
                    reason="set COMMONTRACE_CONFORMANCE_GO=1 with Go installed (downloads golang.org/x/crypto)")
def test_an_independent_go_implementation_written_from_the_spec_passes_three_layers(tmp_path):
    src = os.path.join(ROOT, "protocol", "conformance", "examples", "go")
    binary = tmp_path / "conf"
    subprocess.run(["go", "build", "-o", str(binary), "."], cwd=src, check=True,
                   env={**os.environ, "GOFLAGS": "-mod=mod"})
    results = conformance.run_exec(str(binary), conformance.load_vectors(), only=("assign", "ledger", "digest"))
    assert all(r.ok for r in results), [r.detail for r in results if not r.ok]
