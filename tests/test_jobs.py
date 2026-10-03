"""The durable job queue: enqueue, dedupe, claim with lease, retries with backoff,
dead jobs, handlers, the daemon hook, the CLI, and the batch API's fallback."""
import json
import subprocess
import sys
import time

import pytest

from commontrace import jobs


@pytest.fixture
def root(tmp_path):
    (tmp_path / "memory").mkdir()
    return str(tmp_path)


@pytest.fixture
def flaky(monkeypatch):
    calls = []

    def handler(root, payload):
        calls.append(payload)
        if payload.get("fail_times", 0) >= len(calls):
            raise RuntimeError("transient")
        return {"ok": True, "n": len(calls)}

    monkeypatch.setitem(jobs.HANDLERS, "test", handler)
    monkeypatch.setattr(jobs, "BACKOFF_BASE", 0.0)
    return calls


def test_enqueue_dedupe_and_unknown_kind(root, flaky):
    a = jobs.enqueue(root, "test", {"x": 1}, dedupe_key="k")
    b = jobs.enqueue(root, "test", {"x": 2}, dedupe_key="k")
    assert a.id == b.id and b.payload == {"x": 1}
    with pytest.raises(jobs.JobError):
        jobs.enqueue(root, "nope")
    with pytest.raises(jobs.JobError):
        jobs.enqueue(root, "test", {"blob": "x" * 300_000})


def test_run_retries_then_succeeds(root, flaky):
    job = jobs.enqueue(root, "test", {"fail_times": 2}, max_attempts=3)
    for _ in range(3):
        jobs.run_pending(root)
    done = jobs.get(root, job.id)
    assert done.status == "done" and done.attempts == 3 and done.result["n"] == 3


def test_dead_after_max_attempts_and_retry(root, flaky):
    job = jobs.enqueue(root, "test", {"fail_times": 99}, max_attempts=2)
    jobs.run_pending(root)
    jobs.run_pending(root)
    dead = jobs.get(root, job.id)
    assert dead.status == "dead" and "transient" in dead.error
    assert jobs.retry(root) == 1
    assert jobs.get(root, job.id).status == "queued"


def test_backoff_delays_retry(root, flaky, monkeypatch):
    monkeypatch.setattr(jobs, "BACKOFF_BASE", 100.0)
    job = jobs.enqueue(root, "test", {"fail_times": 1})
    jobs.run_pending(root)
    assert jobs.get(root, job.id).status == "failed"
    assert jobs.claim(root) is None  # not due yet


def test_expired_lease_is_reclaimed(root, flaky):
    job = jobs.enqueue(root, "test", {})
    claimed = jobs.claim(root, lease=-1)
    assert claimed.id == job.id
    again = jobs.claim(root)
    assert again.id == job.id and again.attempts == 2


def test_priority_and_kind_filter(root, flaky, monkeypatch):
    monkeypatch.setitem(jobs.HANDLERS, "other", lambda r, p: "x")
    low = jobs.enqueue(root, "test", {}, priority=0)
    high = jobs.enqueue(root, "test", {}, priority=5)
    jobs.enqueue(root, "other", {})
    assert jobs.claim(root, kinds=["test"]).id == high.id
    assert jobs.claim(root, kinds=["test"]).id == low.id
    assert jobs.claim(root, kinds=["test"]) is None


def test_delay_and_purge(root, flaky):
    job = jobs.enqueue(root, "test", {}, delay=60)
    assert jobs.run_pending(root)["done"] == 0
    assert jobs.get(root, job.id).status == "queued"
    now = jobs.enqueue(root, "test", {})
    jobs.run_pending(root)
    assert jobs.get(root, now.id).status == "done"
    assert jobs.purge(root, older_than_days=-1) == 1


def test_link_and_ingest_handlers(root, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# Runbook\n\nRestart Redis when it times out.\n", encoding="utf-8")
    ingest = jobs.enqueue(root, "ingest", {"source": str(docs), "contextualize": "none"})
    missing = jobs.enqueue(root, "ingest", {"source": str(tmp_path / "missing")}, max_attempts=1)
    link = jobs.enqueue(root, "link", {"spacy": False})
    jobs.run_pending(root)
    assert jobs.get(root, ingest.id).status == "done"
    assert jobs.get(root, missing.id).status == "dead"
    assert jobs.get(root, link.id).status == "done"


def test_run_batch_falls_back_per_item():
    def single(x):
        if x == 3:
            raise ValueError("bad item")
        return x * 2

    assert [r["result"] for r in jobs.run_batch([1, 2], single, lambda xs: [x * 2 for x in xs])] == [2, 4]
    out = jobs.run_batch([1, 2, 3], single, lambda xs: 1 / 0)
    assert [r["ok"] for r in out] == [True, True, False] and out[0]["mode"] == "single"
    wrong_length = jobs.run_batch([1, 2], single, lambda xs: [0])
    assert [r["result"] for r in wrong_length] == [2, 4]
    chunked = jobs.run_batch(list(range(5)), single, lambda xs: [x * 2 for x in xs], chunk=2)
    assert len(chunked) == 5


def test_daemon_runs_queued_jobs(root, flaky):
    from commontrace import daemon

    job = jobs.enqueue(root, "test", {})
    out = daemon.run_once(root)
    assert out["hooks"]["jobs"]["done"] == 1
    assert jobs.get(root, job.id).status == "done"


def test_cli(root):
    def ct(*args):
        return subprocess.run([sys.executable, "-m", "commontrace.cli", "jobs", *args, "--dest", root],  # nosec
                              capture_output=True, text=True, check=False)
    out = ct("add", "link", "--payload", '{"spacy": false}', "--json")
    job_id = json.loads(out.stdout)["id"]
    assert ct("run").returncode == 0
    assert json.loads(ct("show", job_id).stdout)["status"] == "done"
    assert job_id in ct("list").stdout
    assert ct("add", "link", "--payload", "[1]").returncode == 2
    assert time.time()
