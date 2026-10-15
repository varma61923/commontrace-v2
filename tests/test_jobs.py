"""The durable job queue: enqueue, dedupe, claim with lease, retries with backoff,
dead jobs, handlers, the daemon hook, the CLI, and the batch API's fallback."""
import json
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager

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


def test_reclaimed_attempt_cannot_publish_fail_or_renew(root, flaky):
    job = jobs.enqueue(root, "test", {})
    old = jobs.claim(root, lease=-1)
    current = jobs.claim(root)
    assert old.lease_token != current.lease_token
    assert "lease_token" not in current.to_dict()
    for operation in (
        lambda: jobs.complete(root, job.id, "stale", lease_token=old.lease_token),
        lambda: jobs.fail(root, job.id, "stale", lease_token=old.lease_token),
        lambda: jobs.renew(root, job.id, old.lease_token),
    ):
        with pytest.raises(jobs.LeaseLost):
            operation()
    jobs.complete(root, job.id, "current", lease_token=current.lease_token)
    assert jobs.get(root, job.id).result == "current"


def test_retry_does_not_reuse_a_previous_attempt_token(root, flaky):
    job = jobs.enqueue(root, "test", {}, max_attempts=1)
    first = jobs.claim(root)
    assert jobs.fail(root, job.id, "first", lease_token=first.lease_token) == "dead"
    jobs.retry(root, job.id)
    second = jobs.claim(root)
    assert first.attempts == second.attempts == 1
    assert first.lease_token != second.lease_token
    with pytest.raises(jobs.LeaseLost):
        jobs.complete(root, job.id, "stale", lease_token=first.lease_token)
    jobs.complete(root, job.id, "new", lease_token=second.lease_token)


@pytest.mark.parametrize("operation", ["complete", "fail", "renew"])
def test_lease_is_checked_after_waiting_for_the_write_lock(root, flaky, monkeypatch, operation):
    clock = [100.0]
    monkeypatch.setattr(jobs.time, "time", lambda: clock[0])
    jobs.enqueue(root, "test")
    claimed = jobs.claim(root, lease=10)
    original = jobs._write

    @contextmanager
    def delayed_lock(db):
        clock[0] = 111.0
        with original(db):
            yield

    monkeypatch.setattr(jobs, "_write", delayed_lock)
    with pytest.raises(jobs.LeaseLost):
        if operation == "renew":
            jobs.renew(root, claimed.id, claimed.lease_token)
        else:
            getattr(jobs, operation)(root, claimed.id, "late", lease_token=claimed.lease_token)
    assert jobs.get(root, claimed.id).status == "running"


def test_concurrent_workers_claim_each_job_once(root, monkeypatch):
    observed = []
    lock = threading.Lock()

    def handler(root, payload):
        with lock:
            observed.append(payload["id"])
        return {"ok": True}

    monkeypatch.setitem(jobs.HANDLERS, "parallel-test", handler)
    for i in range(40):
        jobs.enqueue(root, "parallel-test", {"id": i})
    with ThreadPoolExecutor(max_workers=4) as workers:
        reports = list(workers.map(lambda _: jobs.run_pending(root, limit=40), range(4)))
    assert sum(report["done"] for report in reports) == 40
    assert sorted(observed) == list(range(40))
    assert jobs.counts(root) == {"done": 40}


def test_worker_crash_loop_stops_at_attempt_budget(root, flaky):
    job = jobs.enqueue(root, "test", {}, max_attempts=2)
    assert jobs.claim(root, lease=-1).attempts == 1
    assert jobs.claim(root, lease=-1).attempts == 2
    assert jobs.claim(root) is None
    stored = jobs.get(root, job.id)
    assert stored.status == "dead" and stored.attempts == 2
    assert "lease expired" in stored.error


def test_running_worker_cannot_overwrite_a_replacement_result(root, monkeypatch):
    def handler(root, payload):
        with closing(jobs._connect(root)) as db:
            db.execute("UPDATE jobs SET lease_until = 0 WHERE status='running'")
        replacement = jobs.claim(root)
        jobs.complete(root, replacement.id, "replacement", lease_token=replacement.lease_token)
        return "old worker"

    monkeypatch.setitem(jobs.HANDLERS, "fencing-test", handler)
    job = jobs.enqueue(root, "fencing-test")
    report = jobs.run_pending(root, limit=1)
    assert report["done"] == report["failed"] == 0
    assert jobs.get(root, job.id).result == "replacement"


def test_live_worker_renews_lease_and_stops_heartbeat(root, monkeypatch):
    clock = [100.0]
    renewed = threading.Event()
    original = jobs.renew
    monkeypatch.setattr(jobs.time, "time", lambda: clock[0])

    def renew(root, job_id, lease_token, *, lease):
        clock[0] = max(clock[0], 100.2)
        original(root, job_id, lease_token, lease=lease)
        renewed.set()

    def handler(root, payload):
        assert renewed.wait(5), "heartbeat must renew while the handler runs"
        clock[0] = 100.4  # beyond the original claim, inside the renewed one
        assert jobs.claim(root) is None
        return "slow work finished"

    monkeypatch.setattr(jobs, "renew", renew)
    monkeypatch.setitem(jobs.HANDLERS, "slow-test", handler)
    job = jobs.enqueue(root, "slow-test")
    assert jobs.run_pending(root, limit=1, lease=0.3)["done"] == 1
    assert jobs.get(root, job.id).attempts == 1
    assert not any(t.name == "commontrace-job-lease" for t in threading.enumerate())


def test_queue_migrates_legacy_schema_without_losing_jobs(root, flaky):
    with sqlite3.connect(jobs.db_path(root)) as db:
        db.executescript(jobs._SCHEMA.replace("    lease_token TEXT,\n", ""))
        db.execute("INSERT INTO jobs (id, kind, payload, run_after, created, updated) VALUES (?, ?, ?, 0, 0, 0)",
                   ("legacy", "test", '{"x": 1}'))
    claimed = jobs.claim(root)
    assert claimed.id == "legacy" and claimed.payload == {"x": 1} and claimed.lease_token
    jobs.complete(root, claimed.id, {"ok": True}, lease_token=claimed.lease_token)
    assert jobs.get(root, "legacy").result == {"ok": True}


def test_oversized_result_is_a_readable_job_failure(root, monkeypatch):
    monkeypatch.setitem(jobs.HANDLERS, "large-result", lambda root, payload: {"body": "x" * 64_000})
    job = jobs.enqueue(root, "large-result", max_attempts=1)
    assert jobs.run_pending(root, limit=1)["failed"] == 1
    stored = jobs.get(root, job.id)
    assert stored.status == "dead" and stored.result is None and "artifact" in stored.error


@pytest.mark.parametrize("lease", [float("nan"), float("inf"), 0, -1])
def test_worker_rejects_invalid_lease(root, lease):
    with pytest.raises(jobs.JobError, match="lease"):
        jobs.run_pending(root, lease=lease)


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
    assert ct("run", "--lease-seconds", "0").returncode == 2
    assert ct("run", "--lease-seconds", "30").returncode == 0
    assert time.time()
