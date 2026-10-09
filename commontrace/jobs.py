"""A durable job queue for slow memory work (document ingestion, model extraction,
summaries, index rebuilds, entity linking, versioning), so a caller can hand work off
and return immediately, and a crash or a failing model does not lose it.

Jobs live in `memory/jobs.db` (SQLite, WAL). A worker claims a job with a lease; a
job whose worker died is reclaimed when its lease runs out. A failure is retried with
exponential backoff up to `max_attempts`, then parked as `dead` for `jobs retry`.
A `dedupe_key` keeps one pending job per key (re-enqueueing the same work is free).

`run_batch` is the batch API: try the whole batch in one call, and when that is
unavailable or fails, fall back to one call per item so one bad item costs only itself."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from commontrace import paths, telemetry

STATUSES = ("queued", "running", "done", "failed", "dead")
DEFAULT_LEASE = 600
BACKOFF_BASE = 5.0
BACKOFF_MAX = 3600.0
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    priority INTEGER NOT NULL DEFAULT 0,
    run_after REAL NOT NULL,
    lease_until REAL,
    dedupe_key TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    result TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, run_after, priority);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedupe ON jobs(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running', 'failed');
"""


class JobError(RuntimeError):
    """A queue operation that cannot proceed."""


@dataclass
class Job:
    id: str
    kind: str
    payload: dict
    status: str
    attempts: int
    max_attempts: int
    priority: int
    run_after: float
    created: float
    updated: float
    result: Any = None
    error: str | None = None
    dedupe_key: str | None = None

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("id", "kind", "payload", "status", "attempts", "max_attempts",
                                              "priority", "run_after", "created", "updated", "result", "error",
                                              "dedupe_key")}


def db_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "jobs.db")


def _connect(root: str) -> sqlite3.Connection:
    from commontrace.conversation.store import connect

    os.makedirs(paths.memory_dir(root), exist_ok=True)
    db = connect(db_path(root), _SCHEMA)
    db.row_factory = sqlite3.Row
    return db


def _write(db: sqlite3.Connection):
    from commontrace.conversation.store import write_txn

    return write_txn(db)


def _job(row: sqlite3.Row) -> Job:
    return Job(row["id"], row["kind"], json.loads(row["payload"]), row["status"], row["attempts"],
               row["max_attempts"], row["priority"], row["run_after"], row["created"], row["updated"],
               json.loads(row["result"]) if row["result"] else None, row["error"], row["dedupe_key"])


def enqueue(root: str, kind: str, payload: dict | None = None, *, dedupe_key: str | None = None,
            delay: float = 0.0, max_attempts: int = 3, priority: int = 0) -> Job:
    """Queue a job; with a `dedupe_key` already pending, return that job instead."""
    if kind not in HANDLERS:
        raise JobError(f"unknown job kind {kind!r}; known: {', '.join(sorted(HANDLERS))}")
    body = json.dumps(payload or {}, sort_keys=True, default=str)
    if len(body) > 256_000:
        raise JobError("job payload is larger than 256 kB; pass a path, not the content")
    now = time.time()
    db = _connect(root)
    try:
        with _write(db):
            if dedupe_key:
                row = db.execute("SELECT * FROM jobs WHERE dedupe_key = ? AND status IN "
                                 "('queued', 'running', 'failed')", (dedupe_key,)).fetchone()
                if row is not None:
                    return _job(row)
            job_id = uuid.uuid4().hex[:16]
            db.execute("INSERT INTO jobs (id, kind, payload, max_attempts, priority, run_after, dedupe_key, "
                       "created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (job_id, kind, body, max(1, min(int(max_attempts), 20)), int(priority), now + max(0.0, delay),
                        dedupe_key, now, now))
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        telemetry.count("commontrace_jobs_enqueued", kind=kind)
        return _job(row)
    finally:
        db.close()


def claim(root: str, *, kinds: Sequence[str] | None = None, lease: float = DEFAULT_LEASE) -> Job | None:
    """Take the next ready job (queued, retry due, or lease expired) for this worker."""
    now = time.time()
    db = _connect(root)
    try:
        with _write(db):
            sql = ("SELECT * FROM jobs WHERE ((status IN ('queued', 'failed') AND run_after <= ?) OR "
                   "(status = 'running' AND lease_until < ?))")
            args: list = [now, now]
            if kinds:
                sql += " AND kind IN (SELECT value FROM json_each(?))"
                args.append(json.dumps(list(kinds)))
            row = db.execute(sql + " ORDER BY priority DESC, run_after, created LIMIT 1", args).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status = 'running', attempts = attempts + 1, lease_until = ?, updated = ? "
                       "WHERE id = ?", (now + lease, now, row["id"]))
            return _job(db.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())
    finally:
        db.close()


def complete(root: str, job_id: str, result: Any = None) -> None:
    db = _connect(root)
    try:
        with _write(db):
            db.execute("UPDATE jobs SET status = 'done', result = ?, error = NULL, lease_until = NULL, updated = ? "
                       "WHERE id = ?", (json.dumps(result, default=str)[:64_000], time.time(), job_id))
    finally:
        db.close()


def fail(root: str, job_id: str, error: str) -> str:
    """Record a failure: retry later with backoff, or park the job as dead."""
    now = time.time()
    db = _connect(root)
    try:
        with _write(db):
            row = db.execute("SELECT attempts, max_attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise JobError(f"no job {job_id}")
            dead = row["attempts"] >= row["max_attempts"]
            delay = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** max(0, row["attempts"] - 1)))
            status = "dead" if dead else "failed"
            db.execute("UPDATE jobs SET status = ?, error = ?, run_after = ?, lease_until = NULL, updated = ? "
                       "WHERE id = ?", (status, error[:4000], now + delay, now, job_id))
        return status
    finally:
        db.close()


def retry(root: str, job_id: str | None = None) -> int:
    """Put dead (or failed) jobs back in the queue now; all dead jobs when no id is given."""
    now = time.time()
    db = _connect(root)
    try:
        with _write(db):
            if job_id:
                cur = db.execute("UPDATE jobs SET status = 'queued', attempts = 0, run_after = ?, updated = ? "
                                 "WHERE id = ? AND status IN ('dead', 'failed')", (now, now, job_id))
            else:
                cur = db.execute("UPDATE jobs SET status = 'queued', attempts = 0, run_after = ?, updated = ? "
                                 "WHERE status = 'dead'", (now, now))
            return cur.rowcount
    finally:
        db.close()


def get(root: str, job_id: str) -> Job | None:
    if not os.path.exists(db_path(root)):
        return None
    db = _connect(root)
    try:
        row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row) if row else None
    finally:
        db.close()


def list_jobs(root: str, status: str | None = None, limit: int = 50) -> list[Job]:
    if not os.path.exists(db_path(root)):
        return []
    if status and status not in STATUSES:
        raise JobError(f"status must be one of {', '.join(STATUSES)}")
    db = _connect(root)
    try:
        sql, args = "SELECT * FROM jobs", []
        if status:
            sql, args = sql + " WHERE status = ?", [status]
        rows = db.execute(sql + " ORDER BY updated DESC LIMIT ?", [*args, max(1, int(limit))]).fetchall()
        return [_job(r) for r in rows]
    finally:
        db.close()


def counts(root: str) -> dict[str, int]:
    if not os.path.exists(db_path(root)):
        return {}
    db = _connect(root)
    try:
        return {r[0]: r[1] for r in db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")}
    finally:
        db.close()


def purge(root: str, *, older_than_days: float = 7.0) -> int:
    """Delete finished jobs older than the cutoff."""
    if not os.path.exists(db_path(root)):
        return 0
    db = _connect(root)
    try:
        with _write(db):
            cur = db.execute("DELETE FROM jobs WHERE status = 'done' AND updated < ?",
                             (time.time() - older_than_days * 86400,))
            return cur.rowcount
    finally:
        db.close()


def run_pending(root: str, *, limit: int = 20, kinds: Sequence[str] | None = None,
                time_budget: float | None = None) -> dict:
    """Process ready jobs until none is left, `limit` is reached, or time runs out."""
    started = time.monotonic()
    done = failed = 0
    for _ in range(max(0, int(limit))):
        if time_budget is not None and time.monotonic() - started > time_budget:
            break
        job = claim(root, kinds=kinds)
        if job is None:
            break
        handler = HANDLERS.get(job.kind)
        with telemetry.bind(job=job.id, job_kind=job.kind), telemetry.span(f"job.{job.kind}") as handle:
            try:
                if handler is None:
                    raise JobError(f"no handler for job kind {job.kind!r}")
                result = handler(root, job.payload)
            except Exception as exc:  # noqa: BLE001 - a job failure is recorded, never raised
                last = traceback.format_exception_only(type(exc), exc)[-1].strip()
                status = fail(root, job.id, last)
                handle.set(outcome=status)
                telemetry.count("commontrace_jobs_failed", kind=job.kind, status=status)
                failed += 1
                continue
            complete(root, job.id, result)
            telemetry.count("commontrace_jobs_done", kind=job.kind)
            done += 1
    return {"done": done, "failed": failed, "pending": counts(root).get("queued", 0)}


# --- batch API ---------------------------------------------------------------------------

def run_batch(items: Sequence[Any], single: Callable[[Any], Any],
              batch: Callable[[Sequence[Any]], Sequence[Any]] | None = None, *,
              chunk: int = 32) -> list[dict]:
    """Process items in chunks with `batch` when given (one call per chunk); a chunk whose
    batch call fails or returns the wrong number of results falls back to `single`
    per item. Each item reports {"ok", "result"} or {"ok": False, "error"}."""
    out: list[dict] = []
    for start in range(0, len(items), max(1, chunk)):
        part = list(items[start:start + max(1, chunk)])
        if batch is not None:
            try:
                results = list(batch(part))
                if len(results) == len(part):
                    out += [{"ok": True, "result": r, "mode": "batch"} for r in results]
                    continue
                telemetry.count("commontrace_batch_fallbacks", reason="length")
            except Exception:  # noqa: BLE001 - fall back to one call per item
                telemetry.count("commontrace_batch_fallbacks", reason="error")
        for item in part:
            try:
                out.append({"ok": True, "result": single(item), "mode": "single"})
            except Exception as exc:  # noqa: BLE001 - one bad item costs only itself
                out.append({"ok": False, "error": f"{type(exc).__name__}: {exc}", "mode": "single"})
    return out


# --- handlers ---------------------------------------------------------------------------

def _ingest(root: str, payload: dict) -> dict:
    from commontrace.ingest.pipeline import create_document_pipeline

    source = payload.get("source")
    if not source or not os.path.exists(source):
        raise JobError(f"ingest source does not exist: {source!r}")
    pipeline = create_document_pipeline(source, root, scope=payload.get("scope", ""), space=payload.get("space"),
                                        contextualize=payload.get("contextualize", "heuristic"),
                                        force=bool(payload.get("force")))
    result = pipeline.run()
    errors = [e for e in result.errors if e not in pipeline.last_warnings]
    if errors:
        raise JobError("; ".join(errors)[:1000])
    return {**result.to_dict(), **pipeline.last_stats}


def _conversation(root: str, payload: dict, op: str) -> dict:
    from commontrace.conversation import Store

    space = payload.get("space") or "default"
    with Store(root, space, create=False) as store:
        if op == "summarize":
            from commontrace.conversation import summary

            return summary.summarize(store, payload.get("sessions"), method=payload.get("method", "extractive"))
        from commontrace import llm
        from commontrace.conversation import extract

        llm.load_config()
        return extract.extract(store, payload.get("sessions"))


def _index(root: str, payload: dict) -> dict:
    from commontrace import semantic_arm

    if not semantic_arm.available():
        raise JobError("the semantic index needs the optional embedding dependencies")
    return {"index": semantic_arm.ensure_fresh(root)}


def _link(root: str, payload: dict) -> dict:
    from commontrace import entities

    return entities.link_lessons(root, payload.get("lessons"), use_spacy=payload.get("spacy"))


def _commit(root: str, payload: dict) -> dict:
    from commontrace import memfs

    return memfs.commit(root, payload.get("message") or "commontrace: memory update")


def _distill(root: str, payload: dict[str, Any]) -> dict[str, Any]:
    from commontrace.distillation_worker import distill_job

    return distill_job(root, payload)


HANDLERS: dict[str, Callable[[str, dict], Any]] = {
    "wiki": lambda root, payload: __import__("commontrace.wiki", fromlist=["refresh"]).refresh(root, payload["id"]),
    "mental-model": lambda root, payload: __import__(
        "commontrace.memory_control", fromlist=["refresh_model"]).refresh_model(root, payload["id"]),
    "ingest": _ingest,
    "extract": lambda root, p: _conversation(root, p, "extract"),
    "summarize": lambda root, p: _conversation(root, p, "summarize"),
    "index": _index,
    "link": _link,
    "commit": _commit,
    "distill": _distill,
}
