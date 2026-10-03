"""The store as a versioned filesystem: validation, the pre-commit hook, conflict
repair, restore and signed handoffs."""
import os
import shutil
import subprocess
import sys

import pytest

from commontrace import memfs, memory_git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

KEY = "k" * 40


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "store"
    (root / "memory" / "lessons").mkdir(parents=True)
    (root / "memory" / "facts.jsonl").write_text('{"id": "a"}\n', encoding="utf-8")
    monkeypatch.setenv("COMMONTRACE_HANDOFF_KEY", KEY)
    memfs.init(str(root))
    return str(root)


def _git(root, *args, check=True):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=check)  # nosec B603 B607


def _append(root, name, text):
    with open(os.path.join(root, "memory", name), "a", encoding="utf-8") as fh:
        fh.write(text)


def test_init_commits_and_installs_hook(store):
    assert memory_git.owns_repo(store)
    assert memfs.status(store)["changed"] == []
    hook = os.path.join(store, ".git", "hooks", "pre-commit")
    assert os.access(hook, os.X_OK)
    with open(os.path.join(store, ".gitignore"), encoding="utf-8") as fh:
        assert ".handoff_key" in fh.read()


def test_validate_enforces_configured_size_limit(store):
    with open(os.path.join(store, "memory", "memfs.json"), "w", encoding="utf-8") as fh:
        fh.write('{"max_file_bytes": 30}')
    with open(os.path.join(store, "memory", "big.txt"), "w", encoding="utf-8") as fh:
        fh.write("x" * 50)
    problems = "\n".join(memfs.validate(store).problems)
    assert "big.txt: 50 bytes; the limit is 30" in problems


def test_validate_reports_each_kind(store):
    _append(store, "facts.jsonl", "not json\n")
    with open(os.path.join(store, "memory", "leak.md"), "w", encoding="utf-8") as fh:
        fh.write("key AKIAIOSFODNN7EXAMPLE\n")
    with open(os.path.join(store, "memory", "c.txt"), "w", encoding="utf-8") as fh:
        fh.write("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> x\n")
    report = memfs.validate(store)
    text = "\n".join(report.problems)
    assert not report.ok
    assert "facts.jsonl:2" in text
    assert "credential" in text
    assert "unresolved merge conflict" in text


def test_commit_refuses_invalid_memory_and_hook_blocks_git(store):
    _append(store, "facts.jsonl", "not json\n")
    with pytest.raises(memfs.MemfsError):
        memfs.commit(store, "bad")
    env = {**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(memfs.__file__)))}
    _git(store, "add", "-A")
    done = subprocess.run(["git", "commit", "-qm", "bad"], cwd=store, capture_output=True, text=True,  # nosec
                          env=env, check=False)
    assert done.returncode != 0
    assert "not a JSON object" in done.stderr
    assert memfs.commit(store, "forced", verify=False)["committed"]


def test_repair_unions_jsonl_and_parks_theirs(store):
    _git(store, "checkout", "-qb", "other")
    _append(store, "facts.jsonl", '{"id": "b"}\n')
    with open(os.path.join(store, "memory", "note.txt"), "w", encoding="utf-8") as fh:
        fh.write("theirs\n")
    memfs.commit(store, "other")
    _git(store, "checkout", "-q", "-")
    _append(store, "facts.jsonl", '{"id": "c"}\n')
    with open(os.path.join(store, "memory", "note.txt"), "w", encoding="utf-8") as fh:
        fh.write("ours\n")
    memfs.commit(store, "ours")
    _git(store, "merge", "other", check=False)
    assert not memfs.validate(store).ok
    out = memfs.repair(store)
    assert out["repaired"] == [os.path.join("memory", "facts.jsonl")]
    with open(os.path.join(store, "memory", "facts.jsonl"), encoding="utf-8") as fh:
        ids = [line for line in fh.read().splitlines() if line]
    assert len(ids) == 3 and len(set(ids)) == 3
    with open(os.path.join(store, "memory", "note.txt"), encoding="utf-8") as fh:
        assert fh.read() == "ours\n"
    assert os.path.exists(os.path.join(store, "memory", "note.txt.theirs"))
    os.remove(os.path.join(store, "memory", "note.txt.theirs"))
    assert memfs.validate(store).ok
    _git(store, "commit", "-qm", "merged")


def test_restore_is_a_new_commit(store):
    first = memory_git.head_hash(store)
    _append(store, "facts.jsonl", '{"id": "b"}\n')
    memfs.commit(store, "b")
    out = memfs.restore(store, first)
    assert out["committed"]
    with open(os.path.join(store, "memory", "facts.jsonl"), encoding="utf-8") as fh:
        assert '"b"' not in fh.read()
    assert len(memory_git.log(store, 10)["entries"]) == 3
    with pytest.raises(memfs.MemfsError):
        memfs.restore(store, "HEAD; rm -rf /")


def test_handoff_round_trip_audience_tamper_drift_and_expiry(store, monkeypatch):
    token = memfs.handoff(store, audience="agent-b", ttl_seconds=600)
    claims = memfs.verify_handoff(store, token, audience="agent-b")
    assert claims["commit"] == memory_git.head_hash(store) and not claims["drift"]
    with pytest.raises(memfs.MemfsError, match="not 'agent-c'"):
        memfs.verify_handoff(store, token, audience="agent-c")
    head, body, sig = token.split(".")
    with pytest.raises(memfs.MemfsError, match="signature"):
        memfs.verify_handoff(store, f"{head}.{body}x.{sig}")
    _append(store, "facts.jsonl", '{"id": "b"}\n')
    with pytest.raises(memfs.MemfsError, match="uncommitted"):
        memfs.handoff(store, audience="agent-b")
    memfs.commit(store, "b")
    assert memfs.verify_handoff(store, token)["drift"] is True
    monkeypatch.setenv("COMMONTRACE_HANDOFF_KEY", "z" * 40)
    with pytest.raises(memfs.MemfsError, match="signature"):
        memfs.verify_handoff(store, token)
    monkeypatch.setattr(memfs.time, "time", lambda: 10**10)
    monkeypatch.setenv("COMMONTRACE_HANDOFF_KEY", KEY)
    with pytest.raises(memfs.MemfsError, match="expired"):
        memfs.verify_handoff(store, token)


def test_keygen_is_private_and_required(store, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_HANDOFF_KEY")
    with pytest.raises(memfs.MemfsError, match="no handoff key"):
        memfs.handoff(store, audience="b")
    path = memfs.keygen(store)
    assert os.stat(path).st_mode & 0o077 == 0
    with pytest.raises(memfs.MemfsError):
        memfs.keygen(store)
    assert memfs.status(store)["changed"] == []  # the key file is ignored
    token = memfs.handoff(store, audience="b")
    assert memfs.verify_handoff(store, token)["aud"] == "b"
    monkeypatch.setenv("COMMONTRACE_HANDOFF_KEY", "short")
    with pytest.raises(memfs.MemfsError, match="at least 32"):
        memfs.handoff(store, audience="b")


def test_cli_memory_commands(store):
    def ct(*args):
        return subprocess.run([sys.executable, "-m", "commontrace.cli", "memory", *args, "--dest", store],  # nosec
                              capture_output=True, text=True, check=False)
    assert ct("status").stdout.startswith("head:")
    _append(store, "facts.jsonl", '{"id": "b"}\n')
    assert "committed" in ct("commit", "-m", "b").stdout
    assert ct("validate").returncode == 0
    token = ct("handoff", "create", "--to", "agent-b").stdout.strip()
    assert token.startswith("cth1.")
    assert "valid handoff" in ct("handoff", "verify", token, "--audience", "agent-b").stdout
    assert ct("handoff", "verify", token, "--audience", "nope").returncode == 1
    assert "b" in ct("log").stdout
