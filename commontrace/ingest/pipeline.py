"""Composable ingestion: Loader -> Transforms -> Submitter, with a no-write preview()."""
from __future__ import annotations

import hashlib
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Sequence

from commontrace.ingest import Chunk, IngestionResult

LAZY_HASH_BYTES = 256 * 1024 * 1024
SAMPLE_BYTES = 1024 * 1024
DOC_SUFFIXES = (".md", ".markdown", ".txt", ".rst", ".adoc", ".org", ".text", ".csv", ".json", ".yaml", ".yml",
                ".toml", ".ini", ".log", ".html", ".htm")


class Loader(ABC):
    """Load raw data from a source."""

    @abstractmethod
    def load(self) -> Iterable[Chunk]:
        """Yield Chunk objects from the source."""
        ...


class Transform(ABC):
    """Transform a stream of chunks. `stats` counts what a transform did, for preview()."""

    stats: dict

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
        would_write.update(self._stats())

        return PreviewReport(chunks=chunks, warnings=warnings, would_write=would_write)

    def _stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for stage in (self.loader, *self.transforms):
            for key, value in (getattr(stage, "stats", None) or {}).items():
                out[key] = out.get(key, 0) + value
        return out

    def run(self) -> IngestionResult:
        """Run the full pipeline and submit to storage; the loader's ledger records
        what was ingested only once the submitter has written it."""
        if self.submitter is None:
            raise ValueError("Pipeline cannot run without a submitter")

        from commontrace import telemetry

        with telemetry.span("ingest.run", submitter=type(self.submitter).__name__) as handle:
            result = self.submitter.submit(self._stream())
            handle.set(chunks=result.chunks_extracted, facts=result.facts_written)
        self.last_warnings = self._collect_warnings()
        result.errors.extend(self.last_warnings)
        commit = getattr(self.loader, "commit", None)
        if commit is not None and not any(e.startswith("fact error") for e in result.errors):
            commit()
        self.last_stats = self._stats()
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


# --- change detection -------------------------------------------------------------

def file_fingerprint(path: str, *, known_sizes: frozenset[int] = frozenset()) -> str:
    """sha256 of a file's bytes; a file over LAZY_HASH_BYTES is sampled (size, head,
    tail) unless another file of the same size is known, when it is hashed in full."""
    size = os.path.getsize(path)
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        if size <= LAZY_HASH_BYTES or size in known_sizes:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
            return "sha256:" + digest.hexdigest()
        digest.update(str(size).encode())
        digest.update(fh.read(SAMPLE_BYTES))
        fh.seek(max(0, size - SAMPLE_BYTES))
        digest.update(fh.read(SAMPLE_BYTES))
    return "sample:" + digest.hexdigest()


class Ledger:
    """What a store has ingested from each file: size, mtime and content fingerprint.
    An unchanged file (same size and mtime) is skipped without being read."""

    def __init__(self, root: str):
        from commontrace import paths

        self.path = os.path.join(paths.memory_dir(root), "ingest_ledger.jsonl")
        self.rows: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        from commontrace import _jsonl

        for row in _jsonl.read_rows(self.path):
            if isinstance(row, dict) and row.get("path"):
                self.rows[row["path"]] = row

    def sizes(self) -> frozenset[int]:
        return frozenset(r.get("size", -1) for r in self.rows.values())

    def changed(self, path: str) -> bool:
        """Whether `path` must be read again; remembers its new fingerprint if so."""
        key = os.path.abspath(path)
        st = os.stat(path)
        known = self.rows.get(key)
        if known and known.get("size") == st.st_size and known.get("mtime_ns") == st.st_mtime_ns:
            return False
        fingerprint = file_fingerprint(path, known_sizes=self.sizes())
        row = {"path": key, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "fingerprint": fingerprint,
               "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if known and known.get("fingerprint") == fingerprint:
            self.pending[key] = row
            return False
        self.pending[key] = row
        return True

    def commit(self) -> None:
        if not self.pending:
            return
        from commontrace import _jsonl

        with _jsonl.locked(self.path):
            current = {r["path"]: r for r in _jsonl.read_rows(self.path) if isinstance(r, dict) and r.get("path")}
            current.update(self.pending)
            _jsonl.write_rows(self.path, list(current.values()))
        self.rows.update(self.pending)
        self.pending.clear()


# --- loaders ----------------------------------------------------------------------

@dataclass
class DirectoryLoader(Loader):
    """Every document under a path (or the one file it names); with a ledger, only
    the files that changed since they were last ingested."""

    path: str
    suffixes: tuple[str, ...] = DOC_SUFFIXES
    max_files: int | None = 1000
    ledger: Ledger | None = None
    stats: dict = field(default_factory=lambda: {"files": 0, "unchanged": 0, "unreadable": 0})

    def load(self) -> Iterable[Chunk]:
        from commontrace.ingest import _read_text, _redact_secrets, _walk_files

        files = [self.path] if os.path.isfile(self.path) else \
            list(_walk_files(self.path, self.suffixes, self.max_files))
        for path in files:
            if self.ledger is not None and not self.ledger.changed(path):
                self.stats["unchanged"] += 1
                continue
            try:
                content = _read_text(path)
            except (OSError, ValueError):
                self.stats["unreadable"] += 1
                continue
            self.stats["files"] += 1
            rel = os.path.relpath(path, self.path) if os.path.isdir(self.path) else os.path.basename(path)
            yield Chunk(content=_redact_secrets(content), source_path=os.path.abspath(path),
                        chunk_id=hashlib.sha256(os.path.abspath(path).encode()).hexdigest()[:16],
                        breadcrumb=rel, chunk_type="document")

    def commit(self) -> None:
        if self.ledger is not None:
            self.ledger.commit()


@dataclass
class TextLoader(Loader):
    """Text handed over directly (stdin, an API body) as one document."""

    text: str
    name: str = "text"

    def load(self) -> Iterable[Chunk]:
        from commontrace.ingest import _redact_secrets

        yield Chunk(content=_redact_secrets(self.text), source_path=self.name,
                    chunk_id=hashlib.sha256(self.text.encode()).hexdigest()[:16],
                    breadcrumb=self.name, chunk_type="document")


# --- transforms ---------------------------------------------------------------------

@dataclass
class InjectionScreen(Transform):
    """Drop a chunk that tries to instruct the agent that will later read it."""

    warnings: list[str] = field(default_factory=list, init=False, repr=False)
    stats: dict = field(default_factory=lambda: {"screened": 0})

    def apply(self, chunks: Iterable[Chunk]) -> Iterable[Chunk]:
        from commontrace import memory_guard

        for chunk in chunks:
            findings = memory_guard.scan_injection(chunk.content)
            if findings:
                self.stats["screened"] += 1
                self.warnings.append(f"chunk {chunk.chunk_id} of {chunk.source_path} dropped: "
                                     + ", ".join(sorted({f.label for f in findings})))
                continue
            yield chunk

    def flush_warnings(self) -> list[str]:
        w = self.warnings.copy()
        self.warnings.clear()
        return w


@dataclass
class Deduplicator(Transform):
    """Drop a chunk whose normalised text was already seen in this run."""

    stats: dict = field(default_factory=lambda: {"duplicates": 0})
    _seen: set = field(default_factory=set, init=False, repr=False)

    def apply(self, chunks: Iterable[Chunk]) -> Iterable[Chunk]:
        for chunk in chunks:
            key = hashlib.sha256(" ".join(chunk.content.lower().split()).encode()).hexdigest()
            if key in self._seen:
                self.stats["duplicates"] += 1
                continue
            self._seen.add(key)
            yield chunk


CONTEXT_PROMPT = """<document>
{document}
</document>
Here is a chunk from that document:
<chunk>
{chunk}
</chunk>
Write one short sentence that situates this chunk within the document, to improve
search retrieval of the chunk. Answer with the sentence only."""


@dataclass
class ContextHeader(Transform):
    """Prefix each chunk with where it sits in its document, so a chunk that says
    "the limit is 30 seconds" is found by a search for the service it is about.
    Heuristic by default (title and section path); with `complete`, a model writes
    the sentence (contextual retrieval), cached by document and chunk hash."""

    complete: Callable | None = None
    root: str | None = None
    max_document: int = 8000
    stats: dict = field(default_factory=lambda: {"headed": 0, "model_calls": 0})
    _documents: dict = field(default_factory=dict, init=False, repr=False)

    def apply(self, chunks: Iterable[Chunk]) -> Iterable[Chunk]:
        for chunk in chunks:
            header = self._header(chunk)
            self.stats["headed"] += 1
            yield Chunk(content=f"[{header}] {chunk.content}", source_path=chunk.source_path,
                        chunk_id=chunk.chunk_id, breadcrumb=chunk.breadcrumb, chunk_type=chunk.chunk_type)

    def _title(self, chunk: Chunk) -> str:
        document = self._document(chunk.source_path)
        m = re.search(r"^#\s+(.+)$", document, re.M)
        return (m.group(1).strip() if m else os.path.splitext(os.path.basename(chunk.source_path))[0])[:120]

    def _document(self, path: str) -> str:
        if path not in self._documents:
            try:
                from commontrace.ingest import _read_text

                self._documents[path] = _read_text(path) if os.path.isfile(path) else ""
            except (OSError, ValueError):
                self._documents[path] = ""
        return self._documents[path]

    def _header(self, chunk: Chunk) -> str:
        heuristic = " › ".join(p for p in (self._title(chunk), chunk.breadcrumb) if p)
        if self.complete is None:
            return heuristic
        document = self._document(chunk.source_path)[:self.max_document] or chunk.content
        key = hashlib.sha256((document + "\x1f" + chunk.content).encode()).hexdigest()
        cached = self._cache().get(key)
        if cached:
            return cached
        try:
            text, _usage = self.complete(CONTEXT_PROMPT.format(document=document, chunk=chunk.content[:4000]))
        except Exception:  # noqa: BLE001 - a model that fails leaves the heuristic header
            return heuristic
        self.stats["model_calls"] += 1
        from commontrace.ingest import contextualize_for_llm

        sentence = contextualize_for_llm(" ".join(text.split()), max_len=300) or heuristic
        self._remember(key, sentence)
        return sentence

    def _cache_path(self) -> str | None:
        if not self.root:
            return None
        from commontrace import paths

        return os.path.join(paths.memory_dir(self.root), "ingest_context_cache.jsonl")

    def _cache(self) -> dict[str, str]:
        if not hasattr(self, "_cache_rows"):
            from commontrace import _jsonl

            path = self._cache_path()
            rows = _jsonl.read_rows(path) if path else []
            self._cache_rows = {r["key"]: r["header"] for r in rows if isinstance(r, dict) and r.get("key")}
        return self._cache_rows

    def _remember(self, key: str, header: str) -> None:
        self._cache()[key] = header
        path = self._cache_path()
        if path:
            from commontrace import _jsonl

            _jsonl.append_row(path, {"key": key, "header": header})


# --- submitters ---------------------------------------------------------------------

@dataclass
class SourceFactSubmitter(Submitter):
    """Write each source file's chunks as facts attributed to that file, retiring the
    facts a re-ingested file no longer states."""

    root: str
    scope: str = ""
    connector: str = "pipeline"

    def submit(self, chunks: Iterable[Chunk]) -> IngestionResult:
        from commontrace.connectors.base import new_run_id, record_chunks

        result = IngestionResult(source_path="pipeline", source_type=self.connector)
        by_source: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            by_source.setdefault(chunk.source_path, []).append(chunk)
            result.chunks_extracted += 1
        run_id = new_run_id()
        for source, items in by_source.items():
            record_chunks(self.root, items, source_id=f"file:{source}", scope=self.scope, run_id=run_id,
                          connector=self.connector, result=result)
        result.source_path = ",".join(sorted(by_source))[:500]
        return result


@dataclass
class ConversationSubmitter(Submitter):
    """Write chunks into a conversation space, one session per document, so recall can
    answer from documents and conversations together."""

    root: str
    space: str
    speaker: str = "document"

    def submit(self, chunks: Iterable[Chunk]) -> IngestionResult:
        from commontrace.conversation import ConversationError, Store

        result = IngestionResult(source_path="pipeline", source_type="conversation")
        by_source: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            by_source.setdefault(chunk.source_path, []).append(chunk)
        with Store(self.root, self.space) as store:
            for source, items in by_source.items():
                session = ("doc:" + os.path.basename(source))[:200]
                try:
                    out = store.add(session, [{"speaker": self.speaker, "role": "document", "text": c.content,
                                               "id": c.chunk_id + ":" + hashlib.sha256(c.content.encode())
                                               .hexdigest()[:8]} for c in items])
                except ConversationError as exc:
                    result.errors.append(f"{source}: {exc}")
                    continue
                result.chunks_extracted += out["added"]
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
        InjectionScreen(),
    ]
    if aliases:
        transforms.append(AliasCanonicalizer(aliases=aliases))
    transforms.append(LimitGuard())

    submitter = MemorySubmitter(root=dest_root, scope=scope)
    return Pipeline(loader=loader, transforms=transforms, submitter=submitter)


def create_document_pipeline(
    source: str,
    dest_root: str,
    *,
    scope: str = "",
    space: str | None = None,
    contextualize: str = "heuristic",
    force: bool = False,
    max_files: int | None = 1000,
    chunk_size: int = 1500,
    overlap: int = 100,
    complete: Callable | None = None,
) -> Pipeline:
    """Documents under `source` -> chunked, screened, deduplicated, contextualised,
    canonicalised with the store's ontology aliases -> facts (or a conversation space).
    Re-running skips files that have not changed since they were ingested."""
    if contextualize not in ("none", "heuristic", "model"):
        raise ValueError("contextualize must be none, heuristic or model")
    ledger = None if force else Ledger(dest_root)
    loader = DirectoryLoader(source, max_files=max_files, ledger=ledger)
    transforms: list[Transform] = [
        TextChunker(max_chars=chunk_size, overlap=overlap),
        InjectionScreen(),
        Deduplicator(),
    ]
    if contextualize != "none":
        if contextualize == "model" and complete is None:
            from commontrace import llm

            llm.load_config()
            complete = llm.complete
        transforms.append(ContextHeader(complete=complete if contextualize == "model" else None, root=dest_root))
    try:
        from commontrace import ontology

        aliases = ontology.load(dest_root).aliases()
    except Exception:  # noqa: BLE001 - no ontology means no alias canonicalisation
        aliases = {}
    if aliases:
        transforms.append(AliasCanonicalizer(aliases=aliases))
    transforms.append(LimitGuard(max_chars=chunk_size * 2))
    submitter: Submitter = ConversationSubmitter(dest_root, space) if space else \
        SourceFactSubmitter(dest_root, scope=scope, connector="documents")
    return Pipeline(loader=loader, transforms=transforms, submitter=submitter)
