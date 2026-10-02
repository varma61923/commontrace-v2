"""Async batching and queueing for ingestion pipeline.

Implements a 7-phase pipeline with fallback mechanisms:
1. Context → 2. Retrieval → 3. Extraction → 4. Embedding →
5. Processing → 6. Persist → 7. Linking

Features:
- Batch API with transparent sequential fallback
- Job queue for async operations with status tracking
- Status states: queued, extracting, chunking, embedding, indexing, done, failed

Based on patterns from Mem0 async batching and Zep queueing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable

from commontrace.ingest import Chunk

# ---------------------------------------------------------------------------
# Job status enumeration
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    """Status states for async ingestion jobs."""
    QUEUED = "queued"
    EXTRACTING = "extracting"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    PERSISTING = "persisting"
    LINKING = "linking"
    DONE = "done"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Job data model
# ---------------------------------------------------------------------------

@dataclass
class Job:
    """A single ingestion job with status tracking."""
    job_id: str
    source_path: str
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    phases_completed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "source_path": self.source_path,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "metadata": self.metadata,
            "phases_completed": self.phases_completed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        return cls(
            job_id=data["job_id"],
            source_path=data["source_path"],
            status=JobStatus(data["status"]),
            created_at=data["created_at"],
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
            metadata=data.get("metadata", {}),
            phases_completed=data.get("phases_completed", []),
        )


# ---------------------------------------------------------------------------
# Job queue
# ---------------------------------------------------------------------------

class JobQueue:
    """In-memory job queue with persistence support."""

    def __init__(self, storage_path: str | None = None):
        self._jobs: dict[str, Job] = {}
        self._storage_path = storage_path
        if storage_path:
            self._load_from_storage()

    def _load_from_storage(self) -> None:
        """Load jobs from persistent storage."""
        if not self._storage_path or not os.path.exists(self._storage_path):
            return
        try:
            with open(self._storage_path, "r") as f:
                data = json.load(f)
                for job_data in data:
                    job = Job.from_dict(job_data)
                    self._jobs[job.job_id] = job
        except Exception:
            # If storage is corrupted, start fresh
            self._jobs = {}

    def _save_to_storage(self) -> None:
        """Save jobs to persistent storage."""
        if not self._storage_path:
            return
        try:
            os.makedirs(os.path.dirname(self._storage_path), exist_ok=True)
            with open(self._storage_path, "w") as f:
                json.dump([job.to_dict() for job in self._jobs.values()], f, indent=2)
        except Exception:
            pass  # Don't fail if storage fails

    def enqueue(self, job: Job) -> None:
        """Add a job to the queue."""
        self._jobs[job.job_id] = job
        self._save_to_storage()

    def get(self, job_id: str) -> Job | None:
        """Get a job by ID."""
        return self._jobs.get(job_id)

    def update_status(self, job_id: str, status: JobStatus, error: str | None = None) -> None:
        """Update job status."""
        job = self._jobs.get(job_id)
        if job:
            job.status = status
            if status != JobStatus.QUEUED and job.started_at is None:
                job.started_at = time.time()
            if status in (JobStatus.DONE, JobStatus.FAILED):
                job.completed_at = time.time()
            if error:
                job.error = error
            self._save_to_storage()

    def add_phase(self, job_id: str, phase: str) -> None:
        """Record a completed phase."""
        job = self._jobs.get(job_id)
        if job and phase not in job.phases_completed:
            job.phases_completed.append(phase)
            self._save_to_storage()

    def list_jobs(self, status: JobStatus | None = None) -> list[Job]:
        """List all jobs, optionally filtered by status."""
        jobs = list(self._jobs.values())
        if status:
            jobs = [j for j in jobs if j.status == status]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def cleanup(self, max_age_seconds: float = 86400) -> int:
        """Remove completed jobs older than max_age_seconds."""
        cutoff = time.time() - max_age_seconds
        to_remove = [
            job_id for job_id, job in self._jobs.items()
            if job.status in (JobStatus.DONE, JobStatus.FAILED)
            and job.completed_at
            and job.completed_at < cutoff
        ]
        for job_id in to_remove:
            del self._jobs[job_id]
        if to_remove:
            self._save_to_storage()
        return len(to_remove)


# ---------------------------------------------------------------------------
# Phase interfaces
# ---------------------------------------------------------------------------

class Phase(ABC):
    """A single phase in the ingestion pipeline."""

    @abstractmethod
    async def execute(self, chunks: Iterable[Chunk], job: Job) -> Iterable[Chunk]:
        """Execute this phase on the chunk stream."""
        ...

    @abstractmethod
    def name(self) -> str:
        """Return the phase name for tracking."""
        ...


# ---------------------------------------------------------------------------
# Batch processor with fallback
# ---------------------------------------------------------------------------

@dataclass
class BatchProcessor:
    """Process chunks in batches with transparent sequential fallback."""

    batch_size: int = 100
    use_batch: bool = True
    fallback_to_sequential: bool = True

    async def process_batch(
        self,
        chunks: Iterable[Chunk],
        processor: Callable[[list[Chunk]], Any],
    ) -> list[Any]:
        """Process chunks in batches, falling back to sequential if batch fails."""
        if not self.use_batch:
            return await self._process_sequential(chunks, processor)

        try:
            return await self._process_in_batches(chunks, processor)
        except Exception as e:
            if self.fallback_to_sequential:
                logging.getLogger("commontrace.ingest.async_pipeline").warning(
                    "Batch processing failed (%s); falling back to sequential processing",
                    e,
                    exc_info=True,
                )
                return await self._process_sequential(chunks, processor)
            raise

    async def _process_in_batches(
        self,
        chunks: Iterable[Chunk],
        processor: Callable[[list[Chunk]], Any],
    ) -> list[Any]:
        """Process in batches."""
        results = []
        batch = []
        for chunk in chunks:
            batch.append(chunk)
            if len(batch) >= self.batch_size:
                result = await self._process_single_batch(batch, processor)
                results.extend(result if isinstance(result, list) else [result])
                batch = []
        if batch:
            result = await self._process_single_batch(batch, processor)
            results.extend(result if isinstance(result, list) else [result])
        return results

    async def _process_single_batch(
        self,
        batch: list[Chunk],
        processor: Callable[[list[Chunk]], Any],
    ) -> Any:
        """Process a single batch."""
        if asyncio.iscoroutinefunction(processor):
            return await processor(batch)
        else:
            # Run sync processor in thread pool
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, processor, batch)

    async def _process_sequential(
        self,
        chunks: Iterable[Chunk],
        processor: Callable[[list[Chunk]], Any],
    ) -> list[Any]:
        """Process sequentially, one chunk at a time."""
        results = []
        for chunk in chunks:
            if asyncio.iscoroutinefunction(processor):
                result = await processor([chunk])
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, processor, [chunk])
            results.extend(result if isinstance(result, list) else [result])
        return results


# ---------------------------------------------------------------------------
# Phased pipeline implementation
# ---------------------------------------------------------------------------

class PhasedPipeline:
    """7-phase ingestion pipeline with async support and job tracking.

    Phases:
    1. Context - Load and prepare context
    2. Retrieval - Retrieve related data
    3. Extraction - Extract entities/relationships
    4. Embedding - Generate embeddings
    5. Processing - Apply transformations
    6. Persist - Write to storage
    7. Linking - Link related items
    """

    def __init__(
        self,
        queue: JobQueue | None = None,
        batch_processor: BatchProcessor | None = None,
    ):
        self.queue = queue or JobQueue()
        self.batch_processor = batch_processor or BatchProcessor()
        self._phases: list[Phase] = []

    def add_phase(self, phase: Phase) -> None:
        """Add a phase to the pipeline."""
        self._phases.append(phase)

    async def run_job(
        self,
        source_path: str,
        chunks: Iterable[Chunk],
        job_id: str | None = None,
    ) -> Job:
        """Run a job through all phases with status tracking."""
        job_id = job_id or str(uuid.uuid4())
        job = Job(job_id=job_id, source_path=source_path)
        self.queue.enqueue(job)

        try:
            self.queue.update_status(job_id, JobStatus.EXTRACTING)

            # Run through all phases
            current_chunks = chunks
            for phase in self._phases:
                phase_name = phase.name()
                self.queue.update_status(
                    job_id,
                    self._status_for_phase(phase_name),
                )
                # Consume the async generator and collect results
                phase_result = []
                async for chunk in phase.execute(current_chunks, job):
                    phase_result.append(chunk)
                current_chunks = phase_result
                self.queue.add_phase(job_id, phase_name)

            self.queue.update_status(job_id, JobStatus.DONE)
            return job

        except Exception as e:
            self.queue.update_status(job_id, JobStatus.FAILED, error=str(e))
            job.error = str(e)
            return job

    def _status_for_phase(self, phase_name: str) -> JobStatus:
        """Map phase name to job status."""
        status_map = {
            "context": JobStatus.QUEUED,
            "retrieval": JobStatus.QUEUED,
            "extraction": JobStatus.EXTRACTING,
            "chunking": JobStatus.CHUNKING,
            "embedding": JobStatus.EMBEDDING,
            "processing": JobStatus.INDEXING,
            "persist": JobStatus.PERSISTING,
            "linking": JobStatus.LINKING,
        }
        return status_map.get(phase_name.lower(), JobStatus.QUEUED)

    async def run_batch(
        self,
        sources: list[tuple[str, Iterable[Chunk]]],
    ) -> list[Job]:
        """Run multiple jobs in parallel."""
        tasks = [
            self.run_job(source_path, chunks)
            for source_path, chunks in sources
        ]
        return await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# Example phase implementations
# ---------------------------------------------------------------------------

class ContextPhase(Phase):
    """Phase 1: Load and prepare context."""

    async def execute(self, chunks: Iterable[Chunk], job: Job) -> Iterable[Chunk]:
        """Prepare context for chunks."""
        for chunk in chunks:
            # Add context metadata
            chunk.metadata = getattr(chunk, "metadata", {})
            chunk.metadata["job_id"] = job.job_id
            chunk.metadata["phase"] = "context"
            yield chunk

    def name(self) -> str:
        return "context"


class RetrievalPhase(Phase):
    """Phase 2: Retrieve relevant prior memories or context for chunks."""

    def __init__(self, retrieve_func: Callable[[str], list[Any]] | None = None):
        self.retrieve_func = retrieve_func

    async def execute(self, chunks: Iterable[Chunk], job: Job) -> Iterable[Chunk]:
        loop = asyncio.get_event_loop()
        for chunk in chunks:
            chunk.metadata = getattr(chunk, "metadata", {})
            if self.retrieve_func:
                try:
                    if asyncio.iscoroutinefunction(self.retrieve_func):
                        retrieved = await self.retrieve_func(chunk.content)
                    else:
                        retrieved = await loop.run_in_executor(None, self.retrieve_func, chunk.content)
                    chunk.metadata["retrieved_context"] = retrieved
                except Exception as exc:
                    chunk.metadata["retrieved_context"] = []
                    chunk.metadata["retrieval_error"] = str(exc)
            yield chunk

    def name(self) -> str:
        return "retrieval"


class ExtractionPhase(Phase):
    """Phase 3: Extract entities and relationships.

    Extracts typed entity candidates from chunk text using
    `commontrace.hierarchical.extract_entities` (or `extract_and_store_entities`
    when root is configured), offloaded to a thread pool executor.
    """

    def __init__(
        self,
        root: str | None = None,
        extract_func: Callable[[str], list[Any]] | None = None,
        store_entities: bool = False,
    ):
        self.root = root
        self.extract_func = extract_func
        self.store_entities = store_entities

    async def execute(self, chunks: Iterable[Chunk], job: Job) -> Iterable[Chunk]:
        """Extract entities from chunks and attach them to chunk metadata."""
        from commontrace.hierarchical import extract_and_store_entities, extract_entities

        loop = asyncio.get_event_loop()

        for chunk in chunks:
            chunk.metadata = getattr(chunk, "metadata", {})
            try:
                if self.extract_func:
                    if asyncio.iscoroutinefunction(self.extract_func):
                        entities = await self.extract_func(chunk.content)
                    else:
                        entities = await loop.run_in_executor(
                            None, self.extract_func, chunk.content
                        )
                elif self.store_entities and self.root:
                    stored = await loop.run_in_executor(
                        None,
                        extract_and_store_entities,
                        self.root,
                        chunk.content,
                        chunk.chunk_id,
                    )
                    entities = [
                        {
                            "type": ent.entity_type,
                            "text": ent.text,
                            "canonical": ent.canonical_name,
                        }
                        for ent in stored
                    ]
                else:
                    raw_entities = await loop.run_in_executor(
                        None, extract_entities, chunk.content
                    )
                    entities = [
                        {"type": etype, "text": etext}
                        for etype, etext in raw_entities
                    ]
                chunk.metadata["entities"] = entities
                chunk.metadata["extracted"] = True
            except Exception as exc:
                chunk.metadata["entities"] = []
                chunk.metadata["extracted"] = False
                chunk.metadata["extraction_error"] = str(exc)

            yield chunk

    def name(self) -> str:
        return "extraction"


class EmbeddingPhase(Phase):
    """Phase 4: Generate embeddings for chunks."""

    def __init__(self, embed_func: Callable[[str], list[float]] | None = None):
        self.embed_func = embed_func

    async def execute(self, chunks: Iterable[Chunk], job: Job) -> Iterable[Chunk]:
        """Generate embeddings for chunks."""
        for chunk in chunks:
            if self.embed_func:
                loop = asyncio.get_event_loop()
                embedding = await loop.run_in_executor(
                    None, self.embed_func, chunk.content
                )
                chunk.metadata = getattr(chunk, "metadata", {})
                chunk.metadata["embedding"] = embedding
            yield chunk

    def name(self) -> str:
        return "embedding"


class ProcessingPhase(Phase):
    """Phase 5: Apply transformations, cleaning, or canonicalization to chunks."""

    def __init__(self, transform_func: Callable[[Chunk], Chunk] | None = None):
        self.transform_func = transform_func

    async def execute(self, chunks: Iterable[Chunk], job: Job) -> Iterable[Chunk]:
        loop = asyncio.get_event_loop()
        for chunk in chunks:
            if self.transform_func:
                if asyncio.iscoroutinefunction(self.transform_func):
                    chunk = await self.transform_func(chunk)
                else:
                    chunk = await loop.run_in_executor(None, self.transform_func, chunk)
            yield chunk

    def name(self) -> str:
        return "processing"


class PersistPhase(Phase):
    """Phase 6: Persist chunks to storage."""

    def __init__(self, storage_func: Callable[[Chunk], Any] | None = None):
        self.storage_func = storage_func

    async def execute(self, chunks: Iterable[Chunk], job: Job) -> Iterable[Chunk]:
        """Persist chunks to storage."""
        for chunk in chunks:
            if self.storage_func:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.storage_func, chunk)
            yield chunk

    def name(self) -> str:
        return "persist"


class LinkingPhase(Phase):
    """Phase 7: Link related items in knowledge graph."""

    def __init__(self, link_func: Callable[[Chunk], Any] | None = None):
        self.link_func = link_func

    async def execute(self, chunks: Iterable[Chunk], job: Job) -> Iterable[Chunk]:
        loop = asyncio.get_event_loop()
        for chunk in chunks:
            if self.link_func:
                if asyncio.iscoroutinefunction(self.link_func):
                    await self.link_func(chunk)
                else:
                    await loop.run_in_executor(None, self.link_func, chunk)
            yield chunk

    def name(self) -> str:
        return "linking"


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

async def create_ingestion_job(
    source_path: str,
    chunks: Iterable[Chunk],
    queue: JobQueue | None = None,
) -> Job:
    """Create and enqueue a new ingestion job."""
    queue = queue or JobQueue()
    job_id = str(uuid.uuid4())
    job = Job(job_id=job_id, source_path=source_path)
    queue.enqueue(job)
    return job


def get_job_status(job_id: str, queue: JobQueue | None = None) -> JobStatus | None:
    """Get the status of a job."""
    queue = queue or JobQueue()
    job = queue.get(job_id)
    return job.status if job else None
