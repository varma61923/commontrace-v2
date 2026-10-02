"""Composable ingestion: Loader -> Transforms -> Submitter, with a no-write preview()."""
from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence

from commontrace.ingest import Chunk, IngestionResult


class Loader(ABC):
    """Load raw data from a source."""

    @abstractmethod
    def load(self) -> Iterable[Chunk]:
        """Yield Chunk objects from the source."""
        ...


class Transform(ABC):
    """Transform a stream of chunks."""

    @abstractmethod
    def apply(self, chunks: Iterable[Chunk]) -> Iterable[Chunk]:
        """Apply transformation to the chunk stream."""
        ...

    def flush_warnings(self) -> list[str]:
        """Flush any accumulated warnings (for stateful transforms)."""
        return []


class Submitter(ABC):
    """Submit processed chunks to storage."""

    @abstractmethod
    def submit(self, chunks: Iterable[Chunk]) -> IngestionResult:
        """Write chunks to storage and return result."""
        ...


@dataclass
class TextChunker(Transform):
    """Split text into chunks at paragraph boundaries with overlap."""
    max_chars: int = 2000
    overlap: int = 50

    def apply(self, chunks: Iterable[Chunk]) -> Iterable[Chunk]:
        for chunk in chunks:
            yield from self._chunk_text(chunk)

    def _chunk_text(self, chunk: Chunk) -> Iterator[Chunk]:
        text = chunk.content
        if len(text) <= self.max_chars:
            yield chunk
            return

        paragraphs: list[str] = []
        for para in text.split("\n\n"):
            while len(para) > self.max_chars:
                cut = para.rfind(" ", 0, self.max_chars)
                cut = cut if cut > self.max_chars // 2 else self.max_chars
                paragraphs.append(para[:cut])
                para = para[cut:]
            paragraphs.append(para)
        current = ""
        chunk_idx = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if len(current) + len(para) + 2 > self.max_chars and current:
                yield Chunk(
                    content=current,
                    source_path=chunk.source_path,
                    chunk_id=f"{chunk.chunk_id}_chunk_{chunk_idx}",
                    breadcrumb=chunk.breadcrumb,
                    chunk_type=chunk.chunk_type,
                )
                chunk_idx += 1
                if self.overlap > 0 and len(current) > self.overlap:
                    current = current[-self.overlap:] + "\n\n"
                else:
                    current = ""

            current += para + "\n\n"

        if current.strip():
            yield Chunk(
                content=current.strip(),
                source_path=chunk.source_path,
                chunk_id=f"{chunk.chunk_id}_chunk_{chunk_idx}",
                breadcrumb=chunk.breadcrumb,
                chunk_type=chunk.chunk_type,
            )


@dataclass
class LLMContextualizer(Transform):
    """Prepare chunks for LLM contextualization."""
    max_len: int = 2000

    def apply(self, chunks: Iterable[Chunk]) -> Iterable[Chunk]:
        from commontrace.ingest import contextualize_for_llm

        for chunk in chunks:
            contextualized = contextualize_for_llm(chunk.content, max_len=self.max_len)
            yield Chunk(
                content=contextualized,
                source_path=chunk.source_path,
                chunk_id=f"{chunk.chunk_id}_ctx",
                breadcrumb=chunk.breadcrumb,
                chunk_type=f"{chunk.chunk_type}_contextualized",
            )


@dataclass
class AliasCanonicalizer(Transform):
    """Canonicalize aliases in chunk content."""
    aliases: dict[str, Sequence[str]] = field(default_factory=dict)
    risky_words: frozenset[str] = frozenset([
        "not", "no", "never", "none", "false", "fail", "error",
        "except", "raise", "assert", "delete", "remove", "drop",
    ])
    warnings: list[str] = field(default_factory=list, init=False, repr=False)

    def apply(self, chunks: Iterable[Chunk]) -> Iterable[Chunk]:
        for chunk in chunks:
            yield self._canonicalize(chunk)

    def _canonicalize(self, chunk: Chunk) -> Chunk:
        content = chunk.content
        changes = []

        for canonical, variants in self.aliases.items():
            for variant in variants:
                pattern = re.compile(rf"(?<![\w-]){re.escape(variant)}(?![\w-])", re.IGNORECASE)
                if not pattern.search(content):
                    continue
                if any(re.search(rf"\b{re.escape(rw)}\b", canonical.lower()) for rw in self.risky_words):
                    self.warnings.append(
                        f"Skipping risky alias expansion: '{variant}' -> '{canonical}' "
                        f"in chunk {chunk.chunk_id}"
                    )
                    continue
                content = pattern.sub(canonical, content)
                changes.append(f"{variant}->{canonical}")

        if changes:
            chunk_id = f"{chunk.chunk_id}_canon"
        else:
            chunk_id = chunk.chunk_id

        return Chunk(
            content=content,
            source_path=chunk.source_path,
            chunk_id=chunk_id,
            breadcrumb=chunk.breadcrumb,
            chunk_type=chunk.chunk_type,
        )

    def flush_warnings(self) -> list[str]:
        w = self.warnings.copy()
        self.warnings.clear()
        return w


@dataclass
class LimitGuard(Transform):
    """Enforce character limits on chunks."""
    max_chars: int = 10000
    warnings: list[str] = field(default_factory=list, init=False, repr=False)

    def apply(self, chunks: Iterable[Chunk]) -> Iterable[Chunk]:
        for chunk in chunks:
            if len(chunk.content) > self.max_chars:
                self.warnings.append(
                    f"Chunk {chunk.chunk_id} truncated from {len(chunk.content)} "
                    f"to {self.max_chars} chars"
                )
                yield Chunk(
                    content=chunk.content[:self.max_chars],
                    source_path=chunk.source_path,
                    chunk_id=chunk.chunk_id,
                    breadcrumb=chunk.breadcrumb,
                    chunk_type=chunk.chunk_type,
                )
            else:
                yield chunk

    def flush_warnings(self) -> list[str]:
        w = self.warnings.copy()
        self.warnings.clear()
        return w


@dataclass
class PreviewReport:
    """Result of a preview() run with no writes."""
    chunks: list[Chunk]
    warnings: list[str] = field(default_factory=list)
    would_write: dict[str, int] = field(default_factory=dict)


class Pipeline:
    """Modular ingestion pipeline: Loader → Transforms → Submitter."""

    def __init__(
        self,
        loader: Loader,
        transforms: Sequence[Transform] = (),
        submitter: Submitter | None = None,
    ):
        self.loader = loader
        self.transforms = transforms
        self.submitter = submitter

    def _stream(self) -> Iterator[Chunk]:
        chunks: Iterable[Chunk] = self.loader.load()
        for transform in self.transforms:
            chunks = transform.apply(chunks)
        return chunks

    def _collect_warnings(self) -> list[str]:
        warnings: list[str] = []
        for transform in self.transforms:
            warnings.extend(transform.flush_warnings())
        return warnings

    def preview(self, limit: int | None = 10) -> PreviewReport:
        """Run the pipeline with no writes."""
        from itertools import islice

        stream = self._stream()
        chunks = list(stream) if limit is None else list(islice(stream, limit))
        warnings = self._collect_warnings()

        if limit is not None:
            warnings.append(
                f"Preview limited to {limit} chunk(s); "
                "use limit=None for exhaustive validation."
            )

        would_write = {
            "chunks": len(chunks),
            "chars": sum(len(c.content) for c in chunks),
        }

        return PreviewReport(chunks=chunks, warnings=warnings, would_write=would_write)

    def run(self) -> IngestionResult:
        """Run the full pipeline and submit to storage."""
        if self.submitter is None:
            raise ValueError("Pipeline cannot run without a submitter")

        stream = self._stream()
        result = self.submitter.submit(stream)
        result.errors.extend(self._collect_warnings())
        return result


@dataclass
class FileLoader(Loader):
    """Load a single text file as a chunk."""

    path: str
    chunk_type: str = "text"

    def load(self) -> Iterable[Chunk]:
        from commontrace.ingest import _fingerprint, _read_text, _redact_secrets

        try:
            content = _read_text(self.path)
        except (OSError, ValueError) as e:
            raise IOError(f"Failed to load file {self.path}: {e}") from e

        yield Chunk(
            content=_redact_secrets(content),
            source_path=self.path,
            chunk_id=_fingerprint(self.path),
            breadcrumb=os.path.basename(self.path),
            chunk_type=self.chunk_type,
        )


@dataclass
class MemorySubmitter(Submitter):
    """Write chunks as atomic facts, with chunk and entity nodes in the graph."""

    root: str
    scope: str = ""

    def submit(self, chunks: Iterable[Chunk]) -> IngestionResult:
        from commontrace import graph as graph_mod
        from commontrace.hierarchical import extract_entities
        from commontrace.ingest import _screened_statement, _write_facts

        result = IngestionResult(source_path="pipeline", source_type="pipeline")
        facts: list[dict] = []
        with graph_mod.batch(self.root):
            for chunk in chunks:
                result.chunks_extracted += 1
                result.source_path = chunk.source_path
                node_id = f"chunk:{chunk.chunk_id}"
                graph_mod.add_node(
                    self.root, node_id, "document",
                    name=chunk.breadcrumb or chunk.chunk_id,
                    properties={"source": chunk.source_path, "chunk_type": chunk.chunk_type},
                )
                result.graph_nodes_written += 1
                for _kind, text in extract_entities(chunk.content):
                    ent_id = "concept:" + re.sub(r"\s+", "_", text.strip().lower())
                    graph_mod.add_node(self.root, ent_id, "concept", name=text)
                    graph_mod.add_edge(self.root, node_id, ent_id, "mentions")
                    result.graph_nodes_written += 1
                    result.graph_edges_written += 1
                statement = _screened_statement(f"{chunk.breadcrumb}: {chunk.content[:300]}", result)
                if statement:
                    facts.append({"statement": statement, "category": "reference",
                                  "scopes": [self.scope] if self.scope else [], "confidence": 0.6})
        _write_facts(self.root, facts, result)
        return result


def create_default_pipeline(
    source_path: str,
    dest_root: str,
    scope: str = "",
    chunk_size: int = 2000,
    overlap: int = 50,
    aliases: dict[str, Sequence[str]] | None = None,
) -> Pipeline:
    """Create a standard ingestion pipeline configured for governed memory."""
    loader = FileLoader(source_path)
    transforms: list[Transform] = [
        TextChunker(max_chars=chunk_size, overlap=overlap),
        LLMContextualizer(),
    ]
    if aliases:
        transforms.append(AliasCanonicalizer(aliases=aliases))
    transforms.append(LimitGuard())

    submitter = MemorySubmitter(root=dest_root, scope=scope)
    return Pipeline(loader=loader, transforms=transforms, submitter=submitter)

