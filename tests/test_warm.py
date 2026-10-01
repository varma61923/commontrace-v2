"""The warm query worker (commontrace/warm.py): faster, and otherwise invisible.

Every test here runs a stand-in for reference/query.py with the same shape
(module-level paths from COMMONTRACE_ROOT, load_model/load_index, a `main()`
that parses argv and prints), so the worker's contract can be checked without
the attention extra: what reaches the caller must be exactly what the
subprocess would have produced, and any failure must land on the subprocess.
"""
import os
import shutil
import signal
import socket
import stat
import tempfile
import textwrap
import time

import pytest

from commontrace import warm
from commontrace.commands import _shellout

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the warm worker is Unix-only")

QUERY = os.path.join("memory", "attention", "query.py")

FAKE_SCRIPT = textwrap.dedent('''\
    """Stand-in for reference/query.py."""
    import argparse
    import os
    import sys

    _ROOT = os.environ.get("COMMONTRACE_ROOT") or "."
    INDEX_PATH = os.path.join(_ROOT, "memory", "attention", "index.npz")
    LESSONS_DIR = os.path.join(_ROOT, "memory", "lessons")
    TELEMETRY_PATH = os.path.join(_ROOT, "memory", "alpha_telemetry.jsonl")
    DEFAULT_MODEL_NAME = "m"
    TRUSTED_MODELS = {"m": ""}
    MODEL_LOADS = []
    INDEX_LOADS = []


    class Ranked:
        def __init__(self, rc, lines=(), stderr=()):
            self.rc, self.lines, self.stderr = rc, list(lines), list(stderr)


    def load_model(model_name=DEFAULT_MODEL_NAME):
        MODEL_LOADS.append(model_name)
        return object()


    def load_index(index_path):
        if not os.path.exists(index_path):
            return Ranked(1, stderr=["no index"])
        INDEX_LOADS.append(index_path)
        with open(index_path, encoding="utf-8") as fh:
            return ("m", fh.read())


    def main():
        parser = argparse.ArgumentParser(description="stand-in")
        parser.add_argument("query")
        parser.add_argument("--top-k", type=int, default=10)
        parser.add_argument("--fail", action="store_true")
        parser.add_argument("--pid", action="store_true")
        args = parser.parse_args()
        load_model()
        index = load_index(INDEX_PATH)
        print("[WARN] a warning, in order", file=sys.stderr)
        if args.fail:
            return 3
        if args.pid:
            print(os.getpid())
            return 0
        print("# " + args.query + " top=" + str(args.top_k))
        print("lessons=" + LESSONS_DIR)
        print("index=" + (index[1] if not isinstance(index, Ranked) else "none"))
        with open(TELEMETRY_PATH, "a", encoding="utf-8") as fh:
            fh.write(args.query + "\\n")
        return 0


    if __name__ == "__main__":
        sys.exit(main())
''')


@pytest.fixture
def env(monkeypatch):
    # A short runtime dir: AF_UNIX paths are capped near 100 bytes, and
    # pytest's own tmp paths are long enough to push past it.
    run = tempfile.mkdtemp(prefix="ctw")
    os.chmod(run, 0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", run)
    monkeypatch.setenv("COMMONTRACE_WARM", "1")
    monkeypatch.setenv("COMMONTRACE_WARM_IDLE", "20")
    yield run
    directory = os.path.join(run, "commontrace")
    for name in os.listdir(directory) if os.path.isdir(directory) else ():
        if name.endswith(".sock"):
            _stop_worker(os.path.join(directory, name))
    shutil.rmtree(run, ignore_errors=True)


def _stop_worker(sock):
    try:
        pid = _ask_pid(sock)
    except OSError:
        pid = None
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _ask_pid(sock):
    reply = warm._ask(sock, {"protocol": warm.PROTOCOL, "argv": ["--pid", "q"], "root": "/"},
                      connect_deadline=0.0)
    return int(reply["stdout"]) if reply and reply["rc"] == 0 else None


FAKE_BUILDER = textwrap.dedent('''\
    """Stand-in for reference/build_index.py."""
    import argparse
    import os
    import sys

    _ROOT = os.environ.get("COMMONTRACE_ROOT") or "."
    LESSONS_DIR = os.path.join(_ROOT, "memory", "lessons")
    INDEX_PATH = os.path.join(_ROOT, "memory", "attention", "index.npz")
    LOADS = []
    PARSES = []


    def _load_frontmatter(fm_text):
        PARSES.append(fm_text)
        return {"name": fm_text}


    class SentenceTransformer:
        def __init__(self, model_name, **kwargs):
            LOADS.append(model_name)


    def main():
        parser = argparse.ArgumentParser(description="stand-in builder")
        parser.add_argument("--force", action="store_true")
        args = parser.parse_args()
        model = SentenceTransformer("m", local_files_only=True)
        for name in sorted(os.listdir(LESSONS_DIR)):
            _load_frontmatter(name)
        print("Loading model m ...")
        with open(INDEX_PATH, "w", encoding="utf-8") as fh:
            fh.write("built" + ("-forced" if args.force else ""))
        print("Index built at " + INDEX_PATH + " from " + LESSONS_DIR)
        return 0


    if __name__ == "__main__":
        sys.exit(main())
''')
BUILD = os.path.join("memory", "attention", "build_index.py")


@pytest.fixture
def script(tmp_path, monkeypatch):
    path = tmp_path / "query.py"
    path.write_text(FAKE_SCRIPT, encoding="utf-8")
    builder = tmp_path / "build_index.py"
    builder.write_text(FAKE_BUILDER, encoding="utf-8")
    monkeypatch.setattr(_shellout, "find_reference_script", lambda root, relative: (
        str(builder) if os.path.basename(relative) == "build_index.py" else str(path)))
    return str(path)


def _store(tmp_path, name, index_text="v1"):
    root = tmp_path / name
    (root / "memory" / "attention").mkdir(parents=True)
    (root / "memory" / "lessons").mkdir(parents=True)
    (root / "memory" / "attention" / "index.npz").write_text(index_text, encoding="utf-8")
    return str(root)


def _both(root, argv, capfd, monkeypatch):
    """(rc, stdout, stderr) through the subprocess, then through the worker."""
    results = []
    for setting in ("0", "1"):
        monkeypatch.setenv("COMMONTRACE_WARM", setting)
        rc, out = _shellout.run_script(root, QUERY, argv, "hint", capture=True)
        results.append((rc, out, capfd.readouterr().err))
    return results


class TestTheWorkerAnswersExactlyAsTheSubprocessWould:
    @pytest.mark.parametrize("argv", [
        ["--top-k", "4", "--", "retry the payment"],
        ["--", "non-ASCII: café — 日本語"],
        ["--fail", "--", "q"],
        ["--top-k", "zero", "--", "q"],  # argparse's own error and exit status 2
        ["--", "--top-k"],
    ])
    def test_same_rc_stdout_and_stderr(self, tmp_path, env, script, capfd, monkeypatch, argv):
        root = _store(tmp_path, "store")
        cold, hot = _both(root, argv, capfd, monkeypatch)
        assert hot == cold
        assert os.listdir(os.path.join(env, "commontrace")), "the worker path was not taken"

    def test_streaming_mode_writes_the_same_output(self, tmp_path, env, script, capfd, monkeypatch):
        root = _store(tmp_path, "store")
        monkeypatch.setenv("COMMONTRACE_WARM", "0")
        assert _shellout.run_script(root, QUERY, ["--", "q"], "hint") == 0
        cold = capfd.readouterr()
        monkeypatch.setenv("COMMONTRACE_WARM", "1")
        assert _shellout.run_script(root, QUERY, ["--", "q"], "hint") == 0
        hot = capfd.readouterr()
        assert (hot.out, hot.err) == (cold.out, cold.err)

    def test_each_request_is_answered_for_its_own_store(self, tmp_path, env, script, capfd):
        a, b = _store(tmp_path, "a", "index-a"), _store(tmp_path, "b", "index-b")
        _, out_a = _shellout.run_script(a, QUERY, ["--", "first"], "hint", capture=True)
        _, out_b = _shellout.run_script(b, QUERY, ["--", "second"], "hint", capture=True)
        assert "index=index-a" in out_a and os.path.join(a, "memory", "lessons") in out_a
        assert "index=index-b" in out_b and os.path.join(b, "memory", "lessons") in out_b
        # Telemetry lands in each store, as the subprocess would have written it.
        assert open(os.path.join(a, "memory", "alpha_telemetry.jsonl")).read() == "first\n"
        assert open(os.path.join(b, "memory", "alpha_telemetry.jsonl")).read() == "second\n"


class TestTheIndexBuilderIsServedToo:
    @pytest.mark.parametrize("argv", [[], ["--force"], ["--bogus"]])
    def test_same_rc_stdout_and_stderr(self, tmp_path, env, script, capfd, monkeypatch, argv):
        results = []
        for setting in ("0", "1"):
            root = _store(tmp_path, "store" + setting)
            monkeypatch.setenv("COMMONTRACE_WARM", setting)
            rc, out = _shellout.run_script(root, BUILD, argv, "hint", capture=True)
            results.append((rc, out.replace(root, "<root>"), capfd.readouterr().err))
            if rc == 0:
                index = os.path.join(root, "memory", "attention", "index.npz")
                assert open(index).read() == "built" + ("-forced" if argv else "")
        assert results[0] == results[1]
        assert os.listdir(os.path.join(env, "commontrace")), "the worker path was not taken"

    def test_it_reuses_the_model_the_query_path_loaded(self, tmp_path, script):
        root = _store(tmp_path, "store")
        loaded = warm._Script(script)
        loaded.answer(["--", "q"], root)          # loads "m" through the query script
        assert loaded.build([], root)["rc"] == 0
        assert loaded.builder().LOADS == []        # not loaded a second time
        assert loaded.build([], root)["rc"] == 0
        assert loaded.builder().LOADS == []

    def test_an_unchanged_lesson_is_parsed_once(self, tmp_path, script):
        root = _store(tmp_path, "store")
        for name in ("lesson_a.md", "lesson_b.md"):
            open(os.path.join(root, "memory", "lessons", name), "w").close()
        loaded = warm._Script(script)
        for _ in range(3):
            assert loaded.build([], root)["rc"] == 0
        assert loaded.builder().PARSES == ["lesson_a.md", "lesson_b.md"]

    def test_an_edited_builder_is_reloaded(self, tmp_path, script):
        root = _store(tmp_path, "store")
        loaded = warm._Script(script)
        first = loaded.builder()
        builder = os.path.join(os.path.dirname(script), "build_index.py")
        with open(builder, "a", encoding="utf-8") as fh:
            fh.write("\n# edited\n")
        assert loaded.builder() is not first
        assert loaded.build([], root)["rc"] == 0


def test_a_long_first_build_is_waited_for_not_run_twice():
    """A first build of a large store outlasts a query's reply timeout; timing
    out would start the same build again in a subprocess."""
    assert warm._reply_timeout({"op": "build"}) > 3600
    assert warm._reply_timeout({"op": "rerank"}) == warm._reply_timeout({}) == warm._REPLY_TIMEOUT_SECONDS


class TestItIsActuallyWarm:
    def test_later_calls_reuse_one_worker(self, tmp_path, env, script, capfd):
        root = _store(tmp_path, "store")
        pids = {_shellout.run_script(root, QUERY, ["--pid", "q"], "hint", capture=True)[1] for _ in range(3)}
        assert len(pids) == 1
        assert int(pids.pop()) != os.getpid()

    def test_model_loads_once_and_index_reloads_only_when_rebuilt(self, tmp_path, script):
        root = _store(tmp_path, "store", "v1")
        loaded = warm._Script(script)
        for _ in range(3):
            assert "index=v1" in loaded.answer(["--", "q"], root)["stdout"]
        assert loaded.module.MODEL_LOADS == ["m"]
        assert len(loaded.module.INDEX_LOADS) == 1
        index = os.path.join(root, "memory", "attention", "index.npz")
        tmp = index + ".new"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("v2-rebuilt")
        os.replace(tmp, index)
        assert "index=v2-rebuilt" in loaded.answer(["--", "q"], root)["stdout"]
        assert len(loaded.module.INDEX_LOADS) == 2

    def test_a_missing_index_is_reported_and_not_cached(self, tmp_path, script):
        root = _store(tmp_path, "store")
        index = os.path.join(root, "memory", "attention", "index.npz")
        os.remove(index)
        loaded = warm._Script(script)
        assert "index=none" in loaded.answer(["--", "q"], root)["stdout"]
        with open(index, "w", encoding="utf-8") as fh:
            fh.write("built")
        assert "index=built" in loaded.answer(["--", "q"], root)["stdout"]


class TestOnlyThisUserCanReachIt:
    def test_directory_and_socket_are_private(self, tmp_path, env, script, capfd):
        _shellout.run_script(_store(tmp_path, "store"), QUERY, ["--", "q"], "hint", capture=True)
        directory = os.path.join(env, "commontrace")
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
        [sock] = [n for n in os.listdir(directory) if n.endswith(".sock")]
        assert stat.S_IMODE(os.stat(os.path.join(directory, sock)).st_mode) == 0o600

    def test_a_directory_others_can_enter_is_refused(self, tmp_path, env, script, capfd, monkeypatch):
        directory = os.path.join(env, "commontrace")
        os.mkdir(directory, 0o700)
        os.chmod(directory, 0o755)
        assert warm.runtime_dir() is None
        # ...and the call still succeeds, through the subprocess.
        rc, out = _shellout.run_script(_store(tmp_path, "store"), QUERY, ["--", "q"], "hint", capture=True)
        assert rc == 0 and "# q" in out
        assert os.listdir(directory) == []

    def test_a_malformed_request_gets_no_answer_and_breaks_nothing(self, tmp_path, env, script, capfd):
        root = _store(tmp_path, "store")
        _shellout.run_script(root, QUERY, ["--", "q"], "hint", capture=True)
        sock = warm.socket_path(script)
        for payload in (b"\x00\x00\x00\x05hello", b"\xff\xff\xff\xff", b"\x00\x00\x00\x02[]"):
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(5)
            conn.connect(sock)
            conn.sendall(payload)
            conn.shutdown(socket.SHUT_WR)
            assert conn.recv(10) == b""
            conn.close()
        assert warm._ask(sock, {"protocol": warm.PROTOCOL, "argv": ["--", "q"], "root": "relative"},
                         connect_deadline=0.0) is None
        rc, out = _shellout.run_script(root, QUERY, ["--", "q"], "hint", capture=True)
        assert rc == 0 and "# q" in out


class TestFailureMeansTheSubprocessNotAnError:
    def test_disabled_never_starts_a_worker(self, tmp_path, env, script, capfd, monkeypatch):
        monkeypatch.setenv("COMMONTRACE_WARM", "0")
        rc, _ = _shellout.run_script(_store(tmp_path, "store"), QUERY, ["--", "q"], "hint", capture=True)
        assert rc == 0
        assert not os.path.exists(os.path.join(env, "commontrace"))

    def test_only_the_query_script_and_its_builder_are_served(self, env):
        assert warm.enabled(QUERY) and warm.enabled(BUILD)
        assert not warm.enabled(os.path.join("benchmark", "measure_performance.py"))

    def test_a_stale_socket_left_by_a_dead_worker_is_replaced(self, tmp_path, env, script, capfd):
        sock = warm.socket_path(script)
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(sock)
        dead.close()  # the file remains; nothing listens on it
        rc, out = _shellout.run_script(_store(tmp_path, "store"), QUERY, ["--", "q"], "hint", capture=True)
        assert rc == 0 and "# q" in out
        assert _ask_pid(sock)

    def test_a_worker_that_cannot_load_the_script_falls_back(self, tmp_path, env, script, capfd, monkeypatch):
        broken = tmp_path / "broken.py"
        broken.write_text("raise ImportError('no torch here')\n", encoding="utf-8")
        monkeypatch.setattr(_shellout, "find_reference_script", lambda root, relative: str(broken))
        rc, _ = _shellout.run_script(_store(tmp_path, "store"), QUERY, ["--", "q"], "hint", capture=True)
        assert rc == 1
        assert "no torch here" in capfd.readouterr().err  # the subprocess's own traceback


class TestItGoesAway:
    def test_after_the_idle_window(self, tmp_path, env, script, capfd, monkeypatch):
        monkeypatch.setenv("COMMONTRACE_WARM_IDLE", "1")
        _shellout.run_script(_store(tmp_path, "store"), QUERY, ["--", "q"], "hint", capture=True)
        sock = warm.socket_path(script)
        assert os.path.exists(sock)
        _wait_for(lambda: not os.path.exists(sock))

    def test_when_the_script_changes_a_new_worker_serves_it(self, tmp_path, env, script, capfd):
        root = _store(tmp_path, "store")
        _shellout.run_script(root, QUERY, ["--", "q"], "hint", capture=True)
        old = warm.socket_path(script)
        old_pid = _ask_pid(old)
        with open(script, "a", encoding="utf-8") as fh:
            fh.write("\n# edited\n")
        assert warm.socket_path(script) != old
        _shellout.run_script(root, QUERY, ["--", "q"], "hint", capture=True)
        new_pid = _ask_pid(warm.socket_path(script))
        assert new_pid and new_pid != old_pid
        _wait_for(lambda: not os.path.exists(old))


def _wait_for(condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.1)
    raise AssertionError("condition not met within %.0fs" % timeout)


class TestDoctorReportsIt:
    def test_status_follows_the_worker(self, tmp_path, env, script, capfd, monkeypatch):
        assert warm.status(script) == (True, "not running; the next semantic query starts it")
        _shellout.run_script(_store(tmp_path, "store"), QUERY, ["--", "q"], "hint", capture=True)
        healthy, detail = warm.status(script)
        assert healthy and detail.startswith("running (")
        monkeypatch.setenv("COMMONTRACE_WARM", "0")
        assert warm.status(script)[1].startswith("off (COMMONTRACE_WARM=0)")

    def test_an_unsafe_directory_is_a_warning(self, env, script):
        directory = os.path.join(env, "commontrace")
        os.mkdir(directory)
        os.chmod(directory, 0o777)
        healthy, detail = warm.status(script)
        assert not healthy and "0700" in detail


# --- the reranker's scores, from the worker (commontrace/rerank_arm.py) ------


class _FakeCrossEncoder:
    def predict(self, pairs, batch_size=64, show_progress_bar=False):
        return [len(task) * 0.5 - len(text) * 0.25 for task, text in pairs]


class TestTheWorkerScoresExactlyAsTheRerankerWould:
    def test_scores_match_in_process_scoring(self, script, monkeypatch):
        from commontrace import rerank_arm

        monkeypatch.setattr(rerank_arm, "_load", lambda *_a: _FakeCrossEncoder())
        loaded = warm._Script(script)
        pairs = [["retry the payment", "Send an idempotency key."], ["q", "x" * 40]]
        assert loaded.rerank("cross-encoder", pairs)["scores"] == _FakeCrossEncoder().predict(pairs)
        assert loaded.rerank("cross-encoder", [])["scores"] == []
        with pytest.raises(ValueError):
            loaded.rerank("not-a-mode", pairs)

    def test_a_malformed_rerank_request_gets_no_answer(self, tmp_path, env, script, capfd):
        _shellout.run_script(_store(tmp_path, "store"), QUERY, ["--", "q"], "hint", capture=True)
        sock = warm.socket_path(script)
        for request in (
            {"op": "rerank", "mode": "cross-encoder", "pairs": [["only one"]]},
            {"op": "rerank", "mode": "cross-encoder", "pairs": "nope"},
            {"op": "rerank", "mode": 3, "pairs": []},
        ):
            assert warm._ask(sock, dict(request, protocol=warm.PROTOCOL), connect_deadline=0.0) is None
        assert _ask_pid(sock)  # still serving

    def test_a_worker_that_cannot_rerank_says_so_and_no_second_worker_starts(
        self, tmp_path, env, script, capfd, monkeypatch
    ):
        _shellout.run_script(_store(tmp_path, "store"), QUERY, ["--", "q"], "hint", capture=True)
        sock = warm.socket_path(script)
        reached, reply = warm._exchange(
            sock, {"protocol": warm.PROTOCOL, "op": "rerank", "mode": "not-a-mode", "pairs": []},
            connect_deadline=0.0)
        assert reached and "error" in reply
        spawned = []
        monkeypatch.setattr(warm, "packaged_query_script", lambda: script)
        monkeypatch.setattr(warm, "_spawn", lambda *a: spawned.append(a) or True)
        assert warm.rerank_scores("not-a-mode", []) is None
        assert spawned == []

    def test_off_means_in_process(self, monkeypatch):
        from commontrace import rerank_arm

        monkeypatch.setenv("COMMONTRACE_WARM", "0")
        assert warm.rerank_scores("cross-encoder", [("a", "b")]) is None
        monkeypatch.setattr(rerank_arm, "_load", lambda *_a: _FakeCrossEncoder())
        monkeypatch.setattr(rerank_arm, "_USE_WORKER", True)
        page, _ = rerank_arm.rerank("task", ["a", "b"], {"a": "short", "b": "a much longer text"}, 2)
        assert [slug for slug, _ in page] == ["a", "b"]

    def test_a_reply_of_the_wrong_length_is_not_trusted(self, monkeypatch):
        monkeypatch.setattr(warm, "_call", lambda script, request, root, env, valid: (
            {"protocol": warm.PROTOCOL, "scores": [1.0]} if valid({"scores": [1.0]}) else None))
        monkeypatch.setenv("COMMONTRACE_WARM", "1")
        assert warm.rerank_scores("cross-encoder", [("a", "b"), ("c", "d")]) is None


def _real_models_cached():
    from commontrace import rerank_arm
    from commontrace.commands import doctor_cmd

    return (
        _shellout.has_attention_deps()
        and doctor_cmd._model_cached(rerank_arm.MODELS["cross-encoder-fast"][0])
    )


@pytest.mark.skipif(not _real_models_cached(), reason="needs the attention extra and a cached cross-encoder")
def test_the_real_model_scores_identically_through_the_worker(env, monkeypatch):
    """The real script and the real (fast) cross-encoder, in a real worker."""
    from commontrace import rerank_arm

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    pairs = [("retry the payment after a timeout", "Send an idempotency key on every payment request."),
             ("retry the payment after a timeout", "Rotate the API key when an engineer leaves.")]
    remote = warm.rerank_scores("cross-encoder-fast", pairs)
    assert remote is not None, "the worker did not answer"
    local = [float(x) for x in rerank_arm._load("cross-encoder-fast").predict(
        pairs, batch_size=64, show_progress_bar=False)]
    assert remote == local
