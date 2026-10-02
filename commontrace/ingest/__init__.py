"""Multimodal ingestion pipeline for CommonTrace.

Adapts the best patterns from Cognee and Supermemory: structured parsing,
entity extraction, and governed indexing into lessons, atomic facts, and
the temporal knowledge graph.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = re.compile(
    r"(?i)(sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9_-]{32,}|AKIA[A-Z0-9]{16}|"
    r"[A-Za-z0-9+/]{32,}={0,2})\b"
)


def _redact_secrets(text: str) -> str:
    return _SECRET_PATTERNS.sub("[REDACTED]", text)


# ---------------------------------------------------------------------------
# LLM contextualizer hardening (prompt-injection tag stripping)
# ---------------------------------------------------------------------------

_MAX_CONTEXT_LEN = 2000

_PAIRED_TAG_RE = re.compile(
    r"<\s*(system|prompt|instruction|context|developer|assistant)\b[^>]*>"
    r".*?"
    r"<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_BARE_TAG_RE = re.compile(
    r"<\s*/?\s*(system|prompt|instruction|context|developer|assistant)\b[^>]*>",
    re.IGNORECASE,
)


def sanitize_contextualizer_text(text: str, max_len: int = _MAX_CONTEXT_LEN) -> str:
    """Strip prompt-injection tags from LLM contextualizer input and cap length.

    Removes paired ``<system>...</system>`` / ``<prompt>...</prompt>``
    (plus instruction/context/developer/assistant) blocks and any bare
    occurrences, then truncates to ``max_len`` chars.
    """
    if not isinstance(text, str):
        text = str(text)
    cleaned = _PAIRED_TAG_RE.sub(" ", text)
    cleaned = _BARE_TAG_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if max_len > 0 and len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned


def contextualize_for_llm(text: str, max_len: int = _MAX_CONTEXT_LEN) -> str:
    """Prepare chunk text for an LLM contextualizer: redact + strip + cap."""
    return sanitize_contextualizer_text(_redact_secrets(text), max_len=max_len)


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Chunk:
    """A bounded text chunk with origin metadata."""
    content: str
    source_path: str
    chunk_id: str
    breadcrumb: str = ""
    chunk_type: str = "text"


@dataclass
class IngestionResult:
    """Results from a single ingestion operation."""
    source_path: str
    source_type: str
    chunks_extracted: int = 0
    facts_written: int = 0
    graph_nodes_written: int = 0
    graph_edges_written: int = 0
    lessons_drafted: int = 0
    traces_written: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_type": self.source_type,
            "chunks_extracted": self.chunks_extracted,
            "facts_written": self.facts_written,
            "graph_nodes": self.graph_nodes_written,
            "graph_edges": self.graph_edges_written,
            "lessons_drafted": self.lessons_drafted,
            "traces_written": self.traces_written,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Code Repository Connector (Cognee AST pattern)
# ---------------------------------------------------------------------------

_MAX_CODE_CHUNK = 2500
_MAX_MD_CHUNK = 2000


def _chunk_code_file(path: str) -> list[Chunk]:
    """Parse a Python file via AST, extract top-level symbols with docstrings."""
    chunks: list[Chunk] = []
    try:
        source = open(path, "r", encoding="utf-8", errors="replace").read()
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        # Fallback: raw text chunking
        for i, block in enumerate(textwrap.wrap(source, _MAX_CODE_CHUNK)):
            chunks.append(Chunk(
                content=_redact_secrets(block),
                source_path=path,
                chunk_id=f"{_fingerprint(path)}_raw_{i}",
                breadcrumb=os.path.basename(path),
                chunk_type="code_raw",
            ))
        return chunks
    except Exception:
        return []

    module_doc = ast.get_docstring(tree) or ""
    if module_doc:
        chunks.append(Chunk(
            content=_redact_secrets(module_doc[:_MAX_CODE_CHUNK]),
            source_path=path,
            chunk_id=f"{_fingerprint(path)}_module_doc",
            breadcrumb=os.path.basename(path),
            chunk_type="module_docstring",
        ))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
            doc = ast.get_docstring(node) or ""
            body = ast.get_source_segment(source, node) or ""
            content = f"{'class' if isinstance(node, ast.ClassDef) else 'def'} {name}:\n"
            if doc:
                content += f'    """{doc[:500]}"""\n'
            content = (content + body)[:_MAX_CODE_CHUNK]
            chunks.append(Chunk(
                content=_redact_secrets(content),
                source_path=path,
                chunk_id=f"{_fingerprint(path)}_{name}",
                breadcrumb=f"{os.path.basename(path)}::{name}",
                chunk_type="code_symbol",
            ))

    return chunks


def ingest_code_repository(
    root: str,
    source_root: str,
    scope: str = "",
    extensions: tuple[str, ...] = (".py",),
    max_files: int = 200,
) -> IngestionResult:
    """Walk a code repository, extract symbols, write graph nodes and candidate lessons."""
    from commontrace import frontmatter, paths
    from commontrace import graph as graph_mod

    result = IngestionResult(source_path=source_root, source_type="code")
    files_processed = 0

    for dirpath, _dirs, filenames in os.walk(source_root):
        for fname in filenames:
            if files_processed >= max_files:
                break
            if not any(fname.endswith(ext) for ext in extensions):
                continue
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, source_root)

            chunks = _chunk_code_file(fpath)
            result.chunks_extracted += len(chunks)

            # Write a graph node per file
            node_id = f"file:{_fingerprint(rel)}"
            graph_mod.add_node(root, node_id, "file", name=rel, properties={"source": source_root})
            result.graph_nodes_written += 1

            for chunk in chunks:
                if chunk.chunk_type == "code_symbol":
                    sym_id = f"symbol:{_fingerprint(chunk.chunk_id)}"
                    graph_mod.add_node(root, sym_id, "symbol", name=chunk.breadcrumb, properties={"source": rel})
                    graph_mod.add_edge(root, node_id, sym_id, "contains")
                    result.graph_nodes_written += 1
                    result.graph_edges_written += 1

                    if len(chunk.content) > 200:
                        # Draft a candidate lesson from substantial symbols
                        lesson_dir = os.path.join(paths.lessons_dir(root))
                        os.makedirs(lesson_dir, exist_ok=True)
                        slug = f"ingest_{_fingerprint(chunk.chunk_id)}"
                        lesson_path = os.path.join(lesson_dir, f"{slug}.md")
                        if not os.path.exists(lesson_path):
                            frontmatter.write(lesson_path, {
                                "title": f"[Code] {chunk.breadcrumb}",
                                "status": "review",
                                "tags": ["ingested", "code"] + ([scope] if scope else []),
                                "scopes": [scope] if scope else [],
                                "source": rel,
                            }, contextualize_for_llm(chunk.content))
                            result.lessons_drafted += 1

            files_processed += 1

    return result


# ---------------------------------------------------------------------------
# Markdown Documentation Connector (hierarchical header chunking)
# ---------------------------------------------------------------------------

def _chunk_markdown(path: str) -> list[Chunk]:
    """Split Markdown by top-level headings into bounded chunks."""
    chunks: list[Chunk] = []
    try:
        text = open(path, "r", encoding="utf-8", errors="replace").read()
    except Exception:
        return []

    sections = re.split(r"(?m)^(#{1,3}\s.+)$", text)
    current_heading = os.path.basename(path)
    current_body = ""

    def flush(heading: str, body: str) -> None:
        body = body.strip()
        if len(body) < 50:
            return
        for i, block in enumerate(textwrap.wrap(body, _MAX_MD_CHUNK)):
            chunks.append(Chunk(
                content=_redact_secrets(block),
                source_path=path,
                chunk_id=f"{_fingerprint(path + heading)}_{i}",
                breadcrumb=heading.strip("# ").strip(),
                chunk_type="markdown_section",
            ))

    for part in sections:
        if re.match(r"^#{1,3}\s", part):
            flush(current_heading, current_body)
            current_heading = part
            current_body = ""
        else:
            current_body += part

    flush(current_heading, current_body)
    return chunks


def ingest_markdown_documentation(
    root: str,
    source_root: str,
    scope: str = "",
    max_files: int = 100,
) -> IngestionResult:
    """Ingest Markdown documentation: extract facts and draft lessons from headings."""
    from commontrace import hierarchical

    result = IngestionResult(source_path=source_root, source_type="markdown")

    for dirpath, _dirs, filenames in os.walk(source_root):
        for fname in filenames:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(dirpath, fname)
            chunks = _chunk_markdown(fpath)
            result.chunks_extracted += len(chunks)

            for chunk in chunks:
                # Headings containing keywords → atomic facts
                bc = chunk.breadcrumb.lower()
                category = "general"
                if any(kw in bc for kw in ("requirement", "constraint", "rule", "must", "should")):
                    category = "constraint"
                elif any(kw in bc for kw in ("prefer", "recommend", "best practice")):
                    category = "preference"
                elif any(kw in bc for kw in ("architecture", "design", "pattern", "structure")):
                    category = "architecture"

                statement = sanitize_contextualizer_text(
                    f"{chunk.breadcrumb}: {chunk.content[:200]}".strip()
                )
                if len(statement) > 30:
                    hierarchical.add_fact(
                        root,
                        statement=statement[:500],
                        category=category,
                        scopes=[scope] if scope else None,
                        confidence=0.7,
                    )
                    result.facts_written += 1

    return result


# ---------------------------------------------------------------------------
# JSON Structured Log Connector (error clustering by fingerprint)
# ---------------------------------------------------------------------------

def ingest_json_logs(
    root: str,
    log_path: str,
    scope: str = "",
    service_name: str = "",
) -> IngestionResult:
    """Parse a JSON log file, cluster errors by fingerprint, write traces and graph nodes."""
    from commontrace import frontmatter, paths
    from commontrace import graph as graph_mod

    result = IngestionResult(source_path=log_path, source_type="json_logs")
    error_buckets: dict[str, list[dict]] = {}

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as lf:
            for line in lf:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                result.chunks_extracted += 1
                level = str(entry.get("level", entry.get("severity", ""))).upper()
                msg = str(entry.get("message", entry.get("msg", entry.get("error", ""))))
                if not msg or level not in ("ERROR", "CRITICAL", "FATAL", "WARNING"):
                    continue
                fp = _fingerprint(re.sub(r"\b\d+\b", "N", msg))
                error_buckets.setdefault(fp, []).append(entry)
    except Exception as exc:
        result.errors.append(f"log parse error: {exc}")
        return result

    # Write graph service node
    svc_id = f"service:{service_name or _fingerprint(log_path)}"
    graph_mod.add_node(root, svc_id, "service", name=service_name or os.path.basename(log_path))
    result.graph_nodes_written += 1

    trace_dir = paths.traces_dir(root)
    os.makedirs(trace_dir, exist_ok=True)

    for fp, entries in error_buckets.items():
        msg = str(entries[0].get("message", entries[0].get("msg", "")))
        error_id = f"error:{fp}"
        graph_mod.add_node(root, error_id, "error", name=msg[:120], properties={"fingerprint": fp})
        graph_mod.add_edge(root, svc_id, error_id, "raises")
        result.graph_nodes_written += 1
        result.graph_edges_written += 1

        # Write an episodic trace for error clusters with ≥2 occurrences
        if len(entries) >= 2:
            slug = f"log_error_{fp}"
            trace_path = os.path.join(trace_dir, f"{slug}.md")
            if not os.path.exists(trace_path):
                context = contextualize_for_llm(f"Recurring error in {service_name or log_path}: {msg}")
                solution = f"Occurred {len(entries)} times. Review service {service_name or log_path}."
                frontmatter.write(trace_path, {
                    "title": f"[Log] {msg[:80]}",
                    "source": log_path,
                    "tags": ["ingested", "log-error"] + ([scope] if scope else []),
                    "scopes": [scope] if scope else [],
                    "fingerprint": fp,
                    "occurrence_count": len(entries),
                }, f"**Context:** {context}\n\n**Solution:** {solution}")
                result.traces_written += 1

    return result


# ---------------------------------------------------------------------------
# Failure Transcript Connector (agent execution turn extraction)
# ---------------------------------------------------------------------------

def ingest_failure_transcript(
    root: str,
    transcript_path: str,
    scope: str = "",
) -> IngestionResult:
    """Parse a JSONL agent transcript, isolate failure turns, draft lessons."""
    from commontrace import frontmatter, paths

    result = IngestionResult(source_path=transcript_path, source_type="transcript")
    turns: list[dict] = []

    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as tf:
            for line in tf:
                line = line.strip()
                if not line:
                    continue
                try:
                    turns.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        result.errors.append(f"transcript parse error: {exc}")
        return result

    result.chunks_extracted = len(turns)

    # Identify failure turns: steps with error status or high-error signals
    failure_turns = [
        t for t in turns
        if str(t.get("status", "")).upper() in ("ERROR", "FAILED", "FAILURE")
        or "error" in str(t.get("content", "")).lower()[:200]
    ]

    lesson_dir = paths.lessons_dir(root)
    os.makedirs(lesson_dir, exist_ok=True)

    for turn in failure_turns[:10]:
        content = str(turn.get("content", ""))[:1000]
        step_idx = turn.get("step_index", "?")
        slug = f"transcript_failure_{_fingerprint(content)}"
        lesson_path = os.path.join(lesson_dir, f"{slug}.md")
        if not os.path.exists(lesson_path):
            frontmatter.write(lesson_path, {
                "title": f"[Transcript] Failure at step {step_idx}",
                "status": "review",
                "tags": ["ingested", "transcript", "failure"] + ([scope] if scope else []),
                "scopes": [scope] if scope else [],
                "source": transcript_path,
                "step_index": step_idx,
            }, contextualize_for_llm(content, max_len=1000))
            result.lessons_drafted += 1

    return result


# ---------------------------------------------------------------------------
# Fact-triple ingestion (subject, predicate, object, valid_at)
# ---------------------------------------------------------------------------

def _load_triples(path_or_list: Any) -> list[dict[str, Any]]:
    """Normalize triples input to a list of dicts.

    Accepts a list of dicts/tuples, or a path to a .json / .jsonl file.
    """
    if isinstance(path_or_list, (list, tuple)):
        items = list(path_or_list)
    elif isinstance(path_or_list, str) and os.path.exists(path_or_list):
        items = []
        with open(path_or_list, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read().strip()
        if not raw:
            return []
        if path_or_list.endswith(".jsonl"):
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except Exception:
                    continue
        else:
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = []
            if isinstance(parsed, dict):
                items = [parsed]
            elif isinstance(parsed, list):
                items = parsed
            else:
                items = []
    else:
        raise ValueError(f"triples source not found: {path_or_list!r}")

    triples: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            triples.append({
                "subject": str(item[0]),
                "predicate": str(item[1]),
                "object": str(item[2]),
                "valid_at": str(item[3]) if len(item) > 3 and item[3] else "",
            })
        elif isinstance(item, dict):
            subj = item.get("subject", "")
            pred = item.get("predicate", item.get("relation", ""))
            obj = item.get("object", item.get("target", ""))
            if subj and pred and obj:
                triples.append({
                    "subject": str(subj),
                    "predicate": str(pred),
                    "object": str(obj),
                    "valid_at": str(item.get("valid_at", item.get("valid_from", "")) or ""),
                })
    return triples


def ingest_fact_triples(
    path_or_list: Any,
    root: str,
    scope: str = "",
    run_id: str = "",
    edge_types: Any = None,
) -> IngestionResult:
    """Ingest (subject, predicate, object, valid_at) triples.

    Writes graph nodes + edges and atomic facts. Returns an IngestionResult.

    ``edge_types`` is an optional allowlist enforcing a predicate naming
    convention: predicates are normalized to snake_case and only those in
    the allowlist are kept as-is; anything else is coerced to
    ``"relates_to"`` (the original predicate is preserved in the edge
    ``properties["predicate"]`` and the fact statement). When ``None``
    (default), the legacy behavior applies: predicates in graph ``RELATIONS``
    are kept, others map to ``"relates_to"``.
    """
    from commontrace import graph as graph_mod
    from commontrace import hierarchical
    from commontrace import ontology as ontology_mod

    label = path_or_list if isinstance(path_or_list, str) else "<triples>"
    result = IngestionResult(source_path=str(label), source_type="fact_triples")
    try:
        triples = _load_triples(path_or_list)
    except Exception as exc:
        result.errors.append(f"triples load error: {exc}")
        return result

    allowed: set[str] | None = None
    if edge_types is not None:
        try:
            allowed = {ontology_mod.normalize_edge_name(e) for e in edge_types}
        except TypeError:
            allowed = {ontology_mod.normalize_edge_name(edge_types)}

    for triple in triples:
        subj = sanitize_contextualizer_text(triple["subject"], max_len=500)
        pred_raw = str(triple["predicate"]).strip().lower()
        obj = sanitize_contextualizer_text(triple["object"], max_len=500)
        valid_at = str(triple.get("valid_at") or "")
        if not subj or not obj or not pred_raw:
            continue
        if allowed is not None:
            pred_norm = ontology_mod.normalize_edge_name(pred_raw)
            relation = pred_norm if pred_norm in allowed else "relates_to"
        else:
            relation = pred_raw if pred_raw in graph_mod.RELATIONS else "relates_to"
        prov = {
            "source_path": str(label),
            "run_id": run_id,
            "detail": {"predicate": pred_raw},
        }
        try:
            graph_mod.add_node(root, subj, "concept", name=subj, provenance=prov)
            result.graph_nodes_written += 1
            graph_mod.add_node(root, obj, "concept", name=obj, provenance=prov)
            result.graph_nodes_written += 1
            graph_mod.add_edge(
                root, subj, obj, relation,
                valid_from=valid_at or None,
                properties={"predicate": pred_raw},
                provenance=prov,
            )
            result.graph_edges_written += 1
            hierarchical.add_fact(
                root,
                statement=f"{subj} {pred_raw} {obj}"[:500],
                category="general",
                scopes=[scope] if scope else None,
                confidence=0.8,
                valid_from=valid_at or None,
            )
            result.facts_written += 1
            result.chunks_extracted += 1
        except Exception as exc:
            result.errors.append(f"triple error ({subj}/{pred_raw}/{obj}): {exc}")

    return result


# ---------------------------------------------------------------------------
# Preview (dry-run, zero writes)
# ---------------------------------------------------------------------------

def preview_ingest(
    path: str,
    source_type: str,
    scope: str = "",
    max_files: int = 200,
    **kwargs: Any,
) -> IngestionResult:
    """Dry-run ingestion: parse/chunk with zero writes.

    Returns an IngestionResult with would-write counts populated and no
    side effects on the store.
    """
    result = IngestionResult(source_path=path, source_type=f"{source_type}:preview")
    stype = source_type.replace("-", "_")
    try:
        if stype == "code":
            files = 0
            for dirpath, _dirs, filenames in os.walk(path):
                for fname in filenames:
                    if files >= max_files:
                        break
                    if not fname.endswith(tuple(kwargs.get("extensions", (".py",)))):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    chunks = _chunk_code_file(fpath)
                    result.chunks_extracted += len(chunks)
                    result.graph_nodes_written += 1  # file node
                    for chunk in chunks:
                        if chunk.chunk_type == "code_symbol":
                            result.graph_nodes_written += 1
                            result.graph_edges_written += 1
                            if len(chunk.content) > 200:
                                result.lessons_drafted += 1
                    files += 1
        elif stype == "markdown":
            files = 0
            for dirpath, _dirs, filenames in os.walk(path):
                for fname in filenames:
                    if not fname.endswith(".md"):
                        continue
                    if files >= max_files:
                        break
                    chunks = _chunk_markdown(os.path.join(dirpath, fname))
                    result.chunks_extracted += len(chunks)
                    result.facts_written += len(
                        [c for c in chunks if len(f"{c.breadcrumb}: {c.content[:200]}".strip()) > 30]
                    )
                    files += 1
        elif stype == "json_logs":
            buckets: dict[str, int] = {}
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as lf:
                    for line in lf:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        result.chunks_extracted += 1
                        level = str(entry.get("level", entry.get("severity", ""))).upper()
                        msg = str(entry.get("message", entry.get("msg", entry.get("error", ""))))
                        if not msg or level not in ("ERROR", "CRITICAL", "FATAL", "WARNING"):
                            continue
                        fp = _fingerprint(re.sub(r"\b\d+\b", "N", msg))
                        buckets[fp] = buckets.get(fp, 0) + 1
            except Exception as exc:
                result.errors.append(f"log parse error: {exc}")
                return result
            result.graph_nodes_written = 1 + len(buckets)  # service + errors
            result.graph_edges_written = len(buckets)
            result.traces_written = len([c for c in buckets.values() if c >= 2])
        elif stype == "transcript":
            turns: list[dict] = []
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as tf:
                    for line in tf:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            turns.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except Exception as exc:
                result.errors.append(f"transcript parse error: {exc}")
                return result
            result.chunks_extracted = len(turns)
            failures = [
                t for t in turns
                if str(t.get("status", "")).upper() in ("ERROR", "FAILED", "FAILURE")
                or "error" in str(t.get("content", "")).lower()[:200]
            ]
            result.lessons_drafted = min(len(failures), 10)
        elif stype == "fact_triples":
            try:
                triples = _load_triples(path)
            except Exception as exc:
                result.errors.append(f"triples load error: {exc}")
                return result
            result.chunks_extracted = len(triples)
            result.graph_nodes_written = 2 * len(triples)
            result.graph_edges_written = len(triples)
            result.facts_written = len(triples)
        elif stype == "multimodal":
            try:
                from commontrace.ingest import multimodal as _mm
            except ImportError as exc:
                result.errors.append(f"multimodal support unavailable: {exc}")
                return result
            targets = [path]
            if os.path.isdir(path):
                targets = [
                    os.path.join(dirpath, fname)
                    for dirpath, _dirs, filenames in os.walk(path)
                    for fname in sorted(filenames)
                ]
            for fpath in targets:
                if os.path.isdir(fpath):
                    continue
                parsed = _mm.ingest_multimodal(fpath)
                result.chunks_extracted += parsed.chunks_extracted
                result.errors.extend(parsed.errors)
                result.facts_written += len([
                    c for c in (getattr(parsed, "chunks", []) or [])
                    if len(f"{c.breadcrumb}: {c.content[:200]}".strip()) > 30
                ])
        else:
            result.errors.append(f"unknown source_type: {source_type!r}")
    except Exception as exc:
        result.errors.append(f"preview error: {exc}")
    return result


# ---------------------------------------------------------------------------
# Multimodal Document Connector (stdlib PDF/DOCX/HTML/image extraction)
# ---------------------------------------------------------------------------

def ingest_multimodal_document(
    root: str,
    source: str,
    scope: str = "",
    max_files: int = 100,
) -> IngestionResult:
    """Ingest PDF/DOCX/HTML/image files: extract facts and draft lessons.

    Single files and directories both accepted; unsupported extensions are
    recorded in ``errors`` without failing the run.
    """
    from commontrace import hierarchical
    from commontrace.ingest import multimodal

    result = IngestionResult(source_path=source, source_type="multimodal")
    targets = [source]
    if os.path.isdir(source):
        targets = []
        seen = 0
        for dirpath, _dirs, filenames in os.walk(source):
            for fname in sorted(filenames):
                if seen >= max_files:
                    break
                targets.append(os.path.join(dirpath, fname))
                seen += 1

    for fpath in targets:
        if os.path.isdir(fpath):
            continue
        parsed = multimodal.ingest_multimodal(fpath)
        result.chunks_extracted += parsed.chunks_extracted
        result.errors.extend(parsed.errors)
        for chunk in getattr(parsed, "chunks", []) or []:
            statement = sanitize_contextualizer_text(
                f"{chunk.breadcrumb}: {chunk.content[:200]}".strip()
            )
            if len(statement) > 30:
                hierarchical.add_fact(
                    root,
                    statement=statement[:500],
                    category="reference",
                    scopes=[scope] if scope else None,
                    confidence=0.6,
                )
                result.facts_written += 1
    return result


# ---------------------------------------------------------------------------
# Unified IngestionPipeline
# ---------------------------------------------------------------------------

class IngestionPipeline:
    """Unified multimodal ingestion pipeline.

    Dispatches to the correct connector based on source_type:
    - 'code': AST-parsed code repository (Cognee pattern)
    - 'markdown': Hierarchical documentation connector (Supermemory pattern)
    - 'json_logs': Structured log clustering connector
    - 'transcript': Agent execution failure transcript connector
    - 'multimodal': PDF/DOCX/HTML/image document connector (stdlib only)
    """

    def ingest_source(
        self,
        path: str,
        source_type: str,
        dest_root: str,
        scope: str = "",
        preview: bool = False,
        **kwargs: Any,
    ) -> IngestionResult:
        """Ingest a source into governed lessons, atomic facts, and the knowledge graph.

        When ``preview`` is True, no writes are performed — would-write
        counts are returned instead.
        """
        stype = source_type.replace("-", "_")
        if preview:
            return preview_ingest(path, stype, scope=scope, **kwargs)
        if stype == "code":
            return ingest_code_repository(dest_root, path, scope=scope, **kwargs)
        elif stype == "markdown":
            return ingest_markdown_documentation(dest_root, path, scope=scope, **kwargs)
        elif stype == "json_logs":
            return ingest_json_logs(dest_root, path, scope=scope, **kwargs)
        elif stype == "transcript":
            return ingest_failure_transcript(dest_root, path, scope=scope, **kwargs)
        elif stype == "fact_triples":
            return ingest_fact_triples(path, dest_root, scope=scope, **kwargs)
        elif stype == "multimodal":
            return ingest_multimodal_document(dest_root, path, scope=scope, **kwargs)
        else:
            result = IngestionResult(source_path=path, source_type=source_type)
            result.errors.append(f"unknown source_type: {source_type!r}")
            return result
