"""Ingest job lifecycle and document catalog.

Provides:
1. Ingest job lifecycle state machine:
   queued -> extracting -> transforming -> embedding -> submitting -> done | failed.
2. Document catalog:
   summary-list vs full-content-get so agents stop dumping entire documents into prompts.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import re
import threading
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from commontrace import _jsonl, paths

INGEST_STAGES = (
    "queued",
    "extracting",
    "transforming",
    "embedding",
    "submitting",
    "done",
    "failed",
)
MAX_DOCUMENT_BYTES = 128 * 1024 * 1024
_DOCUMENT_ID_RE = re.compile(r"[0-9a-f]{16}\Z")
_MANIFEST_CACHE: OrderedDict = OrderedDict()
_MANIFEST_CACHE_LOCK = threading.RLock()
_MAX_CACHED_DOCUMENTS = 50_000
_MAX_MANIFEST_CACHE_BYTES = 16 * 1024 * 1024
_MANIFEST_LOADING: OrderedDict = OrderedDict()
_MANIFEST_GENERATIONS = itertools.count()


def _manifest_signature(info) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)


def _registered_documents(root: str):
    """Immutable, bounded registry cache validated against a pinned manifest."""
    from commontrace.ingest import _open_regular

    path = os.path.abspath(_docs_file(root))
    for _ in range(2):
        with _open_regular(path, MAX_DOCUMENT_BYTES) as fh:
            signature = _manifest_signature(os.fstat(fh.fileno()))
            with _MANIFEST_CACHE_LOCK:
                cached = _MANIFEST_CACHE.get(path)
                if cached is not None and cached[0] == signature:
                    _MANIFEST_CACHE.move_to_end(path)
                    return cached[1], cached[2]
                generation = next(_MANIFEST_GENERATIONS)
                _MANIFEST_LOADING[path] = generation
                _MANIFEST_LOADING.move_to_end(path)
                while len(_MANIFEST_LOADING) > 64:
                    _MANIFEST_LOADING.popitem(last=False)
            payload = fh.read(MAX_DOCUMENT_BYTES + 1)
            if len(payload) > MAX_DOCUMENT_BYTES:
                raise ValueError("document manifest exceeds byte budget")
            if _manifest_signature(os.fstat(fh.fileno())) != signature:
                continue
        ids: set[str] = set()
        sources: dict[str, str] = {}
        for line in payload.splitlines():
            try:
                doc = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(doc, dict):
                continue
            candidate = doc.get("id")
            if not isinstance(candidate, str) or not _DOCUMENT_ID_RE.fullmatch(candidate):
                continue
            ids.add(candidate)
            source = doc.get("source_path")
            if isinstance(source, str):
                sources[source] = candidate
        frozen_ids, frozen_sources = frozenset(ids), MappingProxyType(sources)
        cache_bytes = sum(len(value.encode("utf-8")) for value in ids)
        cache_bytes += sum(len(key.encode("utf-8")) + len(value) for key, value in sources.items())
        # A raced parse is never published as a newer generation. Every next
        # reader checks full identity, so atomic replacements invalidate it.
        with _open_regular(path, MAX_DOCUMENT_BYTES) as current:
            if _manifest_signature(os.fstat(current.fileno())) != signature:
                continue
            with _MANIFEST_CACHE_LOCK:
                if _MANIFEST_LOADING.get(path) == generation:
                    _MANIFEST_LOADING.pop(path, None)
                    _MANIFEST_CACHE.pop(path, None)
                    if (len(ids) + len(sources) <= _MAX_CACHED_DOCUMENTS
                            and cache_bytes <= _MAX_MANIFEST_CACHE_BYTES):
                        _MANIFEST_CACHE[path] = (signature, frozen_ids, frozen_sources, cache_bytes)
                        while (len(_MANIFEST_CACHE) > 8 or
                               sum(len(entry[1]) + len(entry[2]) for entry in _MANIFEST_CACHE.values())
                               > _MAX_CACHED_DOCUMENTS or
                               sum(entry[3] for entry in _MANIFEST_CACHE.values()) > _MAX_MANIFEST_CACHE_BYTES):
                            _MANIFEST_CACHE.popitem(last=False)
        return frozen_ids, frozen_sources
    raise ValueError("document manifest changed during read; retry retrieval")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jobs_file(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "ingest_jobs.jsonl")


def _docs_file(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "documents.jsonl")


def _docs_dir(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "documents")


@dataclass
class IngestJob:
    id: str
    source: str
    stage: str
    progress: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    error: str | None = None
    stats: dict[str, int] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def create_ingest_job(
    root: str,
    source: str,
    job_id: str | None = None,
    options: dict[str, Any] | None = None,
) -> IngestJob:
    """Create a new ingestion job in the 'queued' stage."""
    jid = job_id or f"ingest_{uuid.uuid4().hex[:12]}"
    job = IngestJob(
        id=jid,
        source=str(source),
        stage="queued",
        progress={"current": 0, "total": 0, "pct": 0.0},
        message=f"Job queued for source: {source}",
        stats=dict(options or {}),
        created_at=_now(),
        updated_at=_now(),
    )
    path = _jobs_file(root)
    with _jsonl.locked(path):
        _jsonl.append_row(path, job.to_dict())
    return job


def update_ingest_job(
    root: str,
    job_id: str,
    stage: str,
    progress: dict[str, Any] | None = None,
    message: str = "",
    error: str | None = None,
    stats: dict[str, int] | None = None,
) -> IngestJob:
    """Transition an ingest job to a new stage with updated progress/message/error."""
    if stage not in INGEST_STAGES:
        raise ValueError(f"Invalid stage {stage!r}; must be one of {INGEST_STAGES}")

    path = _jobs_file(root)
    with _jsonl.locked(path):
        rows = _jsonl.read_rows(path)
        found = False
        updated_rows: list[dict[str, Any]] = []
        target_job: IngestJob | None = None

        for row in rows:
            if isinstance(row, dict) and row.get("id") == job_id:
                found = True
                curr = IngestJob(**row)
                if curr.stage in ("done", "failed") and stage != curr.stage:
                    raise ValueError(
                        f"ingest job {job_id!r} is terminal at {curr.stage!r}; "
                        f"cannot transition to {stage!r}"
                    )
                curr.stage = stage
                curr.updated_at = _now()
                if progress is not None:
                    curr.progress = dict(progress)
                if message:
                    curr.message = message
                if error is not None:
                    curr.error = error
                if stats:
                    curr.stats = {**curr.stats, **stats}
                target_job = curr
                updated_rows.append(curr.to_dict())
            else:
                updated_rows.append(row)

        if not found:
            # Create on-the-fly if not found
            target_job = IngestJob(
                id=job_id,
                source="unknown",
                stage=stage,
                progress=dict(progress or {}),
                message=message,
                error=error,
                stats=dict(stats or {}),
            )
            updated_rows.append(target_job.to_dict())

        _jsonl.write_rows(path, updated_rows)
        return target_job  # type: ignore[return-value]


def get_ingest_job(root: str, job_id: str) -> IngestJob | None:
    """Retrieve the current state of an ingest job."""
    path = _jobs_file(root)
    rows = _jsonl.read_rows(path)
    for row in reversed(rows):
        if isinstance(row, dict) and row.get("id") == job_id:
            try:
                return IngestJob(**row)
            except TypeError:
                return None
    return None


def list_ingest_jobs(root: str, limit: int = 50) -> list[IngestJob]:
    """List recent ingest jobs, most recent first."""
    path = _jobs_file(root)
    rows = _jsonl.read_rows(path)
    jobs: list[IngestJob] = []
    seen: set[str] = set()
    for row in reversed(rows):
        if isinstance(row, dict) and row.get("id") and row["id"] not in seen:
            seen.add(row["id"])
            try:
                jobs.append(IngestJob(**row))
            except TypeError:
                continue
            if len(jobs) >= limit:
                break
    return jobs


# --- Document Catalog: Summary-List vs Full-Content-Get -----------------------

@dataclass
class DocumentSummary:
    id: str
    source_path: str
    title: str
    summary: str
    token_count: int
    chunk_count: int
    fingerprint: str
    status: str = "ingested"
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def record_document(
    root: str,
    source_path: str,
    content: str,
    title: str = "",
    chunks: list[str] | None = None,
    status: str = "ingested",
    fingerprint: str = "",
) -> DocumentSummary:
    """Register an ingested document in the catalog and save its full content."""
    clean_path = os.path.abspath(source_path)
    doc_id = hashlib.sha256(clean_path.encode("utf-8")).hexdigest()[:16]
    doc_title = title.strip() or os.path.basename(source_path)

    # Make brief summary (max 200 chars), never the whole document
    first_clean = re.sub(r"\s+", " ", content.strip())
    summary = first_clean[:180] + ("..." if len(first_clean) > 180 else "")

    est_tokens = max(1, math.ceil(len(content) / 4)) if content else 0
    chunk_count = len(chunks) if chunks is not None else 1
    fp = fingerprint or hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    doc = DocumentSummary(
        id=doc_id,
        source_path=clean_path,
        title=doc_title,
        summary=summary,
        token_count=est_tokens,
        chunk_count=chunk_count,
        fingerprint=fp,
        status=status,
        updated_at=_now(),
    )

    # Store the payload atomically before publishing its manifest entry. Pin
    # the directory and replace the leaf rather than following an existing link.
    from commontrace.ingest import _open_directory

    d_dir = _docs_dir(root)
    os.makedirs(d_dir, exist_ok=True)
    payload = json.dumps({
            "id": doc_id,
            "source_path": clean_path,
            "title": doc_title,
            "content": content,
            "chunks": chunks or [content],
            "token_count": est_tokens,
            "fingerprint": fp,
            "updated_at": doc.updated_at,
        }, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"document payload exceeds {MAX_DOCUMENT_BYTES} bytes")
    with _open_directory(d_dir) as directory_fd:
        temp_name = f".{doc_id}.{uuid.uuid4().hex}.tmp"
        fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            path = _docs_file(root)
            with _jsonl.locked(path):
                os.replace(temp_name, f"{doc_id}.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                rows = [r for r in _jsonl.read_rows(path) if isinstance(r, dict) and r.get("id") != doc_id]
                rows.append(doc.to_dict())
                _jsonl.write_rows(path, rows)
        finally:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    return doc


def list_documents(root: str, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """Return lightweight document summaries (never dumping full document bodies)."""
    path = _docs_file(root)
    rows = _jsonl.read_rows(path)
    q = query.strip().lower()
    out: list[dict[str, Any]] = []

    for r in reversed(rows):
        if not isinstance(r, dict) or not r.get("id"):
            continue
        if q:
            target = f"{r.get('title', '')} {r.get('source_path', '')} {r.get('summary', '')}".lower()
            if q not in target:
                continue
        # Ensure full content is NEVER in summary list
        summary_row = {
            "id": r.get("id"),
            "source_path": r.get("source_path"),
            "title": r.get("title"),
            "summary": r.get("summary", "")[:200],
            "token_count": r.get("token_count", 0),
            "chunk_count": r.get("chunk_count", 1),
            "fingerprint": r.get("fingerprint"),
            "status": r.get("status", "ingested"),
            "updated_at": r.get("updated_at"),
        }
        out.append(summary_row)
        if len(out) >= limit:
            break
    return out


def get_document(
    root: str,
    doc_id_or_path: str,
    chunk_index: int | None = None,
) -> dict[str, Any] | None:
    """Fetch a registered payload by ID or source path, without rereading sources."""
    target = doc_id_or_path.strip()
    if not target:
        return None

    # Source paths are identifiers only: explicit prior ingestion authorized
    # their content, but retrieval never opens the source again.
    if os.path.isabs(target) or "/" in target or "\\" in target:
        doc_id = hashlib.sha256(os.path.abspath(target).encode("utf-8")).hexdigest()[:16]
    elif _DOCUMENT_ID_RE.fullmatch(target):
        doc_id = target
    else:
        doc_id = ""

    from commontrace.ingest import _read_bytes
    try:
        ids, sources = _registered_documents(root)
        if doc_id not in ids:
            doc_id = sources.get(target, "")
        if doc_id not in ids:
            return None
        body_path = os.path.join(_docs_dir(root), f"{doc_id}.json")
        data = json.loads(_read_bytes(body_path, MAX_DOCUMENT_BYTES))
    except (OSError, ValueError, RecursionError):
        return None
    if (not isinstance(data, dict) or data.get("id") != doc_id
            or not isinstance(data.get("content"), str) or not isinstance(data.get("chunks"), list)):
        return None

    if chunk_index is not None:
        chunks = data.get("chunks", [])
        if 0 <= chunk_index < len(chunks):
            data["requested_chunk"] = chunks[chunk_index]
            data["chunk_index"] = chunk_index
        else:
            data["requested_chunk"] = None
            data["chunk_index"] = chunk_index
            data["chunk_error"] = f"Chunk index {chunk_index} out of range [0, {len(chunks)-1}]"
    return data
