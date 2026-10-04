"""Ingestion: code, docs, logs, transcripts, triples and documents into governed memory."""
from __future__ import annotations

import ast
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from commontrace import memory_guard
from commontrace.fingerprints import short_fingerprint

_GENERIC_SECRET_RE = re.compile(r"(?<![A-Za-z0-9+/_\-])[A-Za-z0-9+/_\-]{32,}={0,2}(?![A-Za-z0-9+/_\-])")
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"private[_-]?key|passwd|password|bearer)\b(\s*[:=]\s*|\s+)(['\"]?)([^\s'\"]{8,})(['\"]?)"
)


def _looks_random(token: str) -> bool:
    if re.fullmatch(r"[0-9a-f]+", token) or re.fullmatch(r"[0-9A-F]+", token):
        return False
    classes = sum(bool(re.search(p, token)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
    return classes >= 3


_KNOWN_SECRET_RES = tuple(p for _label, p in memory_guard._SECRET_PATTERNS_HIGH) + (
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{10,}"),
)


def _redact_secrets(text: str) -> str:
    if not text:
        return text
    for pattern in _KNOWN_SECRET_RES:
        text = pattern.sub("[REDACTED]", text)
    text = _CREDENTIAL_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]{m.group(5)}", text)
    return _GENERIC_SECRET_RE.sub(lambda m: "[REDACTED]" if _looks_random(m.group(0)) else m.group(0), text)


_MAX_CONTEXT_LEN = 2000

_PAIRED_TAG_RE = re.compile(
    r"<\s*(system|prompt|instruction|context|developer|assistant)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_BARE_TAG_RE = re.compile(
    r"<\s*/?\s*(system|prompt|instruction|context|developer|assistant)\b[^>]*>",
    re.IGNORECASE,
)


def sanitize_contextualizer_text(text: str, max_len: int = _MAX_CONTEXT_LEN) -> str:
    """Strip role-tag blocks, collapse whitespace and cap the length."""
    if not isinstance(text, str):
        text = str(text)
    cleaned = _PAIRED_TAG_RE.sub(" ", text)
    cleaned = _BARE_TAG_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if max_len > 0 and len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned


def contextualize_for_llm(text: str, max_len: int = _MAX_CONTEXT_LEN) -> str:
    """Redact credentials, strip role tags and cap the length."""
    return sanitize_contextualizer_text(_redact_secrets(text), max_len=max_len)


def _fingerprint(text: str) -> str:
    return short_fingerprint(text)


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
    """Counts and errors from one ingestion run."""
    source_path: str
    source_type: str
    chunks_extracted: int = 0
    facts_written: int = 0
    graph_nodes_written: int = 0
    graph_edges_written: int = 0
    lessons_drafted: int = 0
    traces_written: int = 0
    skipped_unchanged: int = 0
    skipped_large: int = 0
    skipped_unsupported: int = 0
    truncated: int = 0
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
            "skipped_unchanged": self.skipped_unchanged,
            "skipped_large": self.skipped_large,
            "skipped_unsupported": self.skipped_unsupported,
            "truncated": self.truncated,
            "errors": self.errors,
        }


_MAX_CODE_CHUNK = 2500
_MAX_MD_CHUNK = 2000
MAX_TEXT_FILE_BYTES = 4 * 1024 * 1024
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", ".tox",
    ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", "site-packages",
    ".idea", ".vscode", ".cache", ".eggs",
})


def _walk_files(source: str, extensions: tuple[str, ...] | None, max_files: int | None,
                *, stats: dict[str, int] | None = None) -> Iterator[str]:
    if os.path.isfile(source):
        if extensions is None or source.lower().endswith(extensions):
            yield source
        return
    count = 0
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.endswith(".egg-info")
            and not os.path.islink(os.path.join(dirpath, d))
        )
        for fname in sorted(filenames):
            if extensions is not None and not fname.lower().endswith(extensions):
                continue
            path = os.path.join(dirpath, fname)
            if os.path.islink(path):
                continue
            if max_files is not None and count >= max_files:
                if stats is not None:
                    stats["truncated"] = stats.get("truncated", 0) + 1
                continue
            count += 1
            yield path


def _read_text(path: str) -> str:
    if os.path.getsize(path) > MAX_TEXT_FILE_BYTES:
        raise ValueError(f"{path!r} is larger than {MAX_TEXT_FILE_BYTES} bytes; skipped")
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _screened_statement(text: str, result: IngestionResult) -> str | None:
    statement = sanitize_contextualizer_text(_redact_secrets(text))[:500].strip()
    if len(statement) <= 30:
        return None
    if memory_guard.scan_injection(statement):
        result.errors.append(f"skipped a fact that reads like a prompt injection: {statement[:80]!r}")
        return None
    return statement


def _write_facts(root: str, items: list[dict[str, Any]], result: IngestionResult) -> None:
    from commontrace import hierarchical

    good: list[dict[str, Any]] = []
    for item in items:
        try:
            hierarchical.prepare_fact(item.get("statement", ""), item.get("category", "general"),
                                      item.get("valid_from"), item.get("valid_until"), item.get("expires_at"))
        except ValueError as exc:
            result.errors.append(f"fact skipped ({str(item.get('statement', ''))[:60]!r}): {exc}")
            continue
        good.append(item)
    if not good:
        return
    try:
        result.facts_written += len(hierarchical.add_facts(root, good))
    except (OSError, ValueError) as exc:
        result.errors.append(f"fact write error: {exc}")


def _ledger_for(root: str, force: bool):
    """Shared ingest ledger for *root*, or None when *force* disables resumption."""
    if force:
        return None
    from commontrace.ingest.pipeline import Ledger

    return Ledger(root)


def _skip_if_unchanged(ledger, fpath: str, result: IngestionResult) -> bool:
    """True when the ledger shows *fpath* was already ingested unchanged."""
    if ledger is None:
        return False
    try:
        unchanged = not ledger.changed(fpath)
    except OSError:
        return False
    if unchanged:
        result.skipped_unchanged += 1
    return unchanged


def _skip_if_large(fpath: str, limit: int, ledger, result: IngestionResult) -> bool:
    """True when *fpath* exceeds *limit*; counted and noted, never silent."""
    try:
        size = os.path.getsize(fpath)
    except OSError:
        return False
    if size <= limit:
        return False
    result.skipped_large += 1
    if ledger is not None:
        ledger.note(fpath, "skipped_large", detail={"bytes": size, "limit": limit})
    return True


def _facts_ok(result: IngestionResult) -> bool:
    """Whether the fact store accepted the run; a store-level failure must not be committed."""
    return not any(e.startswith("fact write error") for e in result.errors)


def _symbol_chunks(path: str, source: str, tree: ast.Module) -> list[Chunk]:
    lines = source.splitlines(keepends=True)
    base = os.path.basename(path)
    chunks: list[Chunk] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = f"{prefix}{child.name}"
                start = min([d.lineno for d in child.decorator_list] + [child.lineno]) - 1
                body = "".join(lines[start:getattr(child, "end_lineno", child.lineno)])
                kind = "class" if isinstance(child, ast.ClassDef) else "def"
                doc = ast.get_docstring(child) or ""
                header = f"{kind} {qualname}:\n" + (f'    """{doc[:500]}"""\n' if doc else "")
                chunks.append(Chunk(
                    content=_redact_secrets((header + body)[:_MAX_CODE_CHUNK]),
                    source_path=path,
                    chunk_id=f"{_fingerprint(path)}_{qualname}",
                    breadcrumb=f"{base}::{qualname}",
                    chunk_type="code_symbol",
                ))
                visit(child, qualname + ".")

    visit(tree, "")
    return chunks


def _chunk_code_file(path: str) -> list[Chunk]:
    try:
        source = _read_text(path)
    except (OSError, ValueError):
        return []
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return [
            Chunk(
                content=_redact_secrets(source[i:i + _MAX_CODE_CHUNK]),
                source_path=path,
                chunk_id=f"{_fingerprint(path)}_raw_{n}",
                breadcrumb=os.path.basename(path),
                chunk_type="code_raw",
            )
            for n, i in enumerate(range(0, len(source), _MAX_CODE_CHUNK))
        ]
    chunks: list[Chunk] = []
    module_doc = ast.get_docstring(tree) or ""
    if module_doc:
        chunks.append(Chunk(
            content=_redact_secrets(module_doc[:_MAX_CODE_CHUNK]),
            source_path=path,
            chunk_id=f"{_fingerprint(path)}_module_doc",
            breadcrumb=os.path.basename(path),
            chunk_type="module_docstring",
        ))
    chunks.extend(_symbol_chunks(path, source, tree))
    return chunks


def ingest_code_repository(
    root: str,
    source_root: str,
    scope: str = "",
    extensions: tuple[str, ...] = (".py",),
    max_files: int = 200,
    *,
    force: bool = False,
) -> IngestionResult:
    """Map a code repository into the graph (files contain symbols) and module facts.

    Resumable: files unchanged since the last ingest are skipped via the ledger;
    oversized files and max_files truncation are counted, never silent."""
    from commontrace import graph as graph_mod

    result = IngestionResult(source_path=source_root, source_type="code")
    ledger = _ledger_for(root, force)
    walk_stats: dict[str, int] = {}
    facts: list[dict[str, Any]] = []
    with graph_mod.batch(root):
        for fpath in _walk_files(source_root, tuple(extensions), max_files, stats=walk_stats):
            if _skip_if_unchanged(ledger, fpath, result):
                continue
            if _skip_if_large(fpath, MAX_TEXT_FILE_BYTES, ledger, result):
                continue
            rel = os.path.relpath(fpath, source_root) if os.path.isdir(source_root) else os.path.basename(fpath)
            chunks = _chunk_code_file(fpath)
            result.chunks_extracted += len(chunks)
            file_id = f"file:{_fingerprint(rel)}"
            graph_mod.add_node(root, file_id, "file", name=rel, properties={"source": source_root})
            result.graph_nodes_written += 1
            for chunk in chunks:
                if chunk.chunk_type == "module_docstring":
                    statement = _screened_statement(f"{rel}: {chunk.content[:300]}", result)
                    if statement:
                        facts.append({"statement": statement, "category": "architecture",
                                      "scopes": [scope] if scope else [], "confidence": 0.7})
                elif chunk.chunk_type == "code_symbol":
                    sym_id = f"symbol:{_fingerprint(chunk.chunk_id)}"
                    graph_mod.add_node(root, sym_id, "symbol", name=chunk.breadcrumb, properties={"source": rel})
                    graph_mod.add_edge(root, file_id, sym_id, "contains")
                    result.graph_nodes_written += 1
                    result.graph_edges_written += 1
            if ledger is not None:
                ledger.note(fpath, "ingested")
    _write_facts(root, facts, result)
    result.truncated += walk_stats.get("truncated", 0)
    if ledger is not None and _facts_ok(result):
        ledger.commit()
    return result


def _chunk_markdown_text(text: str, source_path: str, default_heading: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    sections = re.split(r"(?m)^(#{1,3}\s.+)$", text)
    heading = default_heading
    body = ""

    def flush(h: str, b: str) -> None:
        b = b.strip()
        if len(b) < 50:
            return
        for i, start in enumerate(range(0, len(b), _MAX_MD_CHUNK)):
            chunks.append(Chunk(
                content=_redact_secrets(b[start:start + _MAX_MD_CHUNK]),
                source_path=source_path,
                chunk_id=f"{_fingerprint(source_path + h)}_{i}",
                breadcrumb=h.strip("# ").strip()[:200],
                chunk_type="markdown_section",
            ))

    for part in sections:
        if re.match(r"^#{1,3}\s", part):
            flush(heading, body)
            heading, body = part, ""
        else:
            body += part
    flush(heading, body)
    return chunks


def _chunk_markdown(path: str) -> list[Chunk]:
    try:
        text = _read_text(path)
    except (OSError, ValueError):
        return []
    return _chunk_markdown_text(text, path, os.path.basename(path))


def categorize_heading(breadcrumb: str) -> str:
    bc = (breadcrumb or "").lower()
    if any(kw in bc for kw in ("requirement", "constraint", "rule", "must", "should")):
        return "constraint"
    if any(kw in bc for kw in ("prefer", "recommend", "best practice")):
        return "preference"
    if any(kw in bc for kw in ("architecture", "design", "pattern", "structure")):
        return "architecture"
    return "general"


def ingest_markdown_documentation(
    root: str,
    source_root: str,
    scope: str = "",
    max_files: int = 100,
    *,
    force: bool = False,
) -> IngestionResult:
    """Turn Markdown sections into atomic facts, categorised by their heading.

    Resumable: files unchanged since the last ingest are skipped via the ledger;
    oversized files and max_files truncation are counted, never silent."""
    result = IngestionResult(source_path=source_root, source_type="markdown")
    ledger = _ledger_for(root, force)
    walk_stats: dict[str, int] = {}
    facts: list[dict[str, Any]] = []
    for fpath in _walk_files(source_root, (".md", ".markdown"), max_files, stats=walk_stats):
        if _skip_if_unchanged(ledger, fpath, result):
            continue
        if _skip_if_large(fpath, MAX_TEXT_FILE_BYTES, ledger, result):
            continue
        chunks = _chunk_markdown(fpath)
        result.chunks_extracted += len(chunks)
        for chunk in chunks:
            statement = _screened_statement(f"{chunk.breadcrumb}: {chunk.content[:200]}", result)
            if statement:
                facts.append({"statement": statement, "category": categorize_heading(chunk.breadcrumb),
                              "scopes": [scope] if scope else [], "confidence": 0.7})
        if ledger is not None:
            ledger.note(fpath, "ingested")
    _write_facts(root, facts, result)
    result.truncated += walk_stats.get("truncated", 0)
    if ledger is not None and _facts_ok(result):
        ledger.commit()
    return result


_LOG_LEVELS = ("ERROR", "CRITICAL", "FATAL", "WARNING")


def _log_buckets(log_path: str, result: IngestionResult) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            result.chunks_extracted += 1
            level = str(entry.get("level", entry.get("severity", ""))).upper()
            msg = str(entry.get("message", entry.get("msg", entry.get("error", ""))))
            if not msg or level not in _LOG_LEVELS:
                continue
            fp = _fingerprint(re.sub(r"\b\d+\b", "N", msg))
            bucket = buckets.setdefault(fp, {"count": 0, "message": msg[:2000]})
            bucket["count"] += 1
    return buckets


def ingest_json_logs(
    root: str,
    log_path: str,
    scope: str = "",
    service_name: str = "",
    *,
    force: bool = False,
) -> IngestionResult:
    """Cluster log errors by fingerprint into the graph; recurring ones become traces.

    Resumable: an unchanged log file is skipped via the ledger."""
    from commontrace import graph as graph_mod
    from commontrace import trace_io

    result = IngestionResult(source_path=log_path, source_type="json_logs")
    ledger = _ledger_for(root, force)
    if _skip_if_unchanged(ledger, log_path, result):
        return result
    if _skip_if_large(log_path, MAX_TEXT_FILE_BYTES, ledger, result):
        if ledger is not None:
            ledger.commit()
        return result
    try:
        buckets = _log_buckets(log_path, result)
    except OSError as exc:
        result.errors.append(f"log parse error: {exc}")
        return result
    service = service_name or os.path.basename(log_path)
    svc_id = f"service:{service_name or _fingerprint(log_path)}"
    with graph_mod.batch(root):
        graph_mod.add_node(root, svc_id, "service", name=service)
        result.graph_nodes_written += 1
        for fp, bucket in buckets.items():
            msg = _redact_secrets(bucket["message"])
            error_id = f"error:{fp}"
            graph_mod.add_node(root, error_id, "error", name=msg[:120],
                               properties={"fingerprint": fp, "occurrences": bucket["count"]})
            graph_mod.add_edge(root, svc_id, error_id, "raises")
            result.graph_nodes_written += 1
            result.graph_edges_written += 1
    for fp, bucket in buckets.items():
        if bucket["count"] < 2:
            continue
        msg = bucket["message"]
        try:
            written = trace_io.write_new(
                root,
                title=f"Recurring error in {service}: {sanitize_contextualizer_text(msg, 150)}",
                context=contextualize_for_llm(f"Recurring error in {service}: {msg}"),
                solution=f"Seen {bucket['count']} times in {os.path.basename(log_path)}; not yet resolved.",
                tags=["ingested", "log-error", *([scope] if scope else [])],
                trace_id=f"log-error-{fp}",
                outcome={"resolved": False},
            )
        except (OSError, ValueError) as exc:
            result.errors.append(f"trace write error for {fp}: {exc}")
            continue
        if written:
            result.traces_written += 1
    if ledger is not None:
        ledger.note(log_path, "ingested" if not result.errors else "error")
        ledger.commit()
    return result


def _failure_turns(transcript_path: str, result: IngestionResult) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                turn = json.loads(line)
            except ValueError:
                continue
            if isinstance(turn, dict):
                turns.append(turn)
    result.chunks_extracted = len(turns)
    return [
        t for t in turns
        if str(t.get("status", "")).upper() in ("ERROR", "FAILED", "FAILURE")
        or re.search(r"\b(error|exception|traceback|failed)\b", str(t.get("content", ""))[:300], re.I)
    ]


def ingest_failure_transcript(
    root: str,
    transcript_path: str,
    scope: str = "",
    *,
    force: bool = False,
) -> IngestionResult:
    """Record each failing turn of a JSONL agent transcript as a trace for distillation.

    Resumable: an unchanged transcript is skipped via the ledger."""
    from commontrace import trace_io

    result = IngestionResult(source_path=transcript_path, source_type="transcript")
    ledger = _ledger_for(root, force)
    if _skip_if_unchanged(ledger, transcript_path, result):
        return result
    if _skip_if_large(transcript_path, MAX_TEXT_FILE_BYTES, ledger, result):
        if ledger is not None:
            ledger.commit()
        return result
    try:
        failures = _failure_turns(transcript_path, result)
    except OSError as exc:
        result.errors.append(f"transcript parse error: {exc}")
        return result
    for turn in failures[:50]:
        content = str(turn.get("content", ""))[:4000]
        step = turn.get("step_index", "?")
        fp = _fingerprint(f"{transcript_path}|{step}|{content}")
        try:
            written = trace_io.write_new(
                root,
                title=f"Agent failure at step {step}: {sanitize_contextualizer_text(content, 120)}",
                context=contextualize_for_llm(content, max_len=4000) or "(empty turn)",
                solution="Not yet resolved; review and capture the fix that worked.",
                tags=["ingested", "transcript", "failure", *([scope] if scope else [])],
                trace_id=f"transcript-{fp}",
                outcome={"resolved": False},
            )
        except (OSError, ValueError) as exc:
            result.errors.append(f"trace write error at step {step}: {exc}")
            continue
        if written:
            result.traces_written += 1
    if ledger is not None:
        ledger.note(transcript_path, "ingested" if not result.errors else "error")
        ledger.commit()
    return result


def _load_triples(path_or_list: Any) -> list[dict[str, Any]]:
    if isinstance(path_or_list, (list, tuple)):
        items = list(path_or_list)
    elif isinstance(path_or_list, str) and os.path.isfile(path_or_list):
        raw = _read_text(path_or_list).strip()
        if not raw:
            return []
        items = []
        if path_or_list.endswith(".jsonl"):
            for line in raw.splitlines():
                try:
                    items.append(json.loads(line))
                except ValueError:
                    continue
        else:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = []
            items = [parsed] if isinstance(parsed, dict) else parsed if isinstance(parsed, list) else []
    else:
        raise ValueError(f"triples source not found: {path_or_list!r}")

    triples: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            triples.append({
                "subject": str(item[0]), "predicate": str(item[1]), "object": str(item[2]),
                "valid_at": str(item[3]) if len(item) > 3 and item[3] else "",
            })
        elif isinstance(item, dict):
            subj = item.get("subject", "")
            pred = item.get("predicate", item.get("relation", ""))
            obj = item.get("object", item.get("target", ""))
            if subj and pred and obj:
                triples.append({
                    "subject": str(subj), "predicate": str(pred), "object": str(obj),
                    "valid_at": str(item.get("valid_at", item.get("valid_from", "")) or ""),
                })
    return triples


def normalize_edge_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")


def ingest_fact_triples(
    path_or_list: Any,
    root: str,
    scope: str = "",
    run_id: str = "",
    edge_types: Any = None,
    *,
    force: bool = False,
) -> IngestionResult:
    """(subject, predicate, object, valid_at) triples into graph edges and atomic facts.

    Resumable when the triples come from a file: an unchanged file is skipped via the ledger."""
    from commontrace import graph as graph_mod

    label = path_or_list if isinstance(path_or_list, str) else "<triples>"
    result = IngestionResult(source_path=str(label), source_type="fact_triples")
    ledger = _ledger_for(root, force) if isinstance(path_or_list, str) else None
    if isinstance(path_or_list, str):
        if _skip_if_unchanged(ledger, path_or_list, result):
            return result
        if _skip_if_large(path_or_list, MAX_TEXT_FILE_BYTES, ledger, result):
            if ledger is not None:
                ledger.commit()
            return result
    try:
        triples = _load_triples(path_or_list)
    except (OSError, ValueError) as exc:
        result.errors.append(f"triples load error: {exc}")
        return result
    allowed: set[str] | None = None
    if edge_types is not None:
        values = [edge_types] if isinstance(edge_types, str) else list(edge_types)
        allowed = {normalize_edge_name(e) for e in values}
    facts: list[dict[str, Any]] = []
    with graph_mod.batch(root):
        for triple in triples:
            subj = sanitize_contextualizer_text(_redact_secrets(triple["subject"]), max_len=500)
            obj = sanitize_contextualizer_text(_redact_secrets(triple["object"]), max_len=500)
            pred_raw = str(triple["predicate"]).strip().lower()
            if not subj or not obj or not pred_raw:
                continue
            pred = normalize_edge_name(pred_raw)
            if allowed is not None:
                relation = pred if pred in allowed else graph_mod.FALLBACK_RELATION
            else:
                relation = pred if pred in graph_mod.RELATIONS else graph_mod.FALLBACK_RELATION
            prov = {"source_path": str(label), "run_id": run_id, "detail": {"predicate": pred_raw}}
            try:
                graph_mod.add_node(root, subj, "concept", name=subj, provenance=prov)
                graph_mod.add_node(root, obj, "concept", name=obj, provenance=prov)
                graph_mod.add_edge(root, subj, obj, relation, valid_at=triple.get("valid_at") or None,
                                   properties={"predicate": pred_raw}, provenance=prov)
            except ValueError as exc:
                result.errors.append(f"triple error ({subj}/{pred_raw}/{obj}): {exc}")
                continue
            result.graph_nodes_written += 2
            result.graph_edges_written += 1
            result.chunks_extracted += 1
            statement = f"{subj} {pred_raw} {obj}"[:500]
            if memory_guard.scan_injection(statement):
                result.errors.append(f"skipped a fact that reads like a prompt injection: {statement[:80]!r}")
                continue
            facts.append({"statement": statement, "category": "general",
                          "scopes": [scope] if scope else [], "confidence": 0.8,
                          "valid_from": triple.get("valid_at") or None})
    _write_facts(root, facts, result)
    if ledger is not None and isinstance(path_or_list, str):
        ledger.note(path_or_list, "ingested" if _facts_ok(result) and not result.errors else "error")
        if _facts_ok(result):
            ledger.commit()
    return result


def _multimodal_targets(source: str, max_files: int | None, stats: dict[str, int] | None = None) -> list[str]:
    from commontrace.ingest import multimodal

    if os.path.isfile(source):
        return [source]
    return list(_walk_files(source, tuple(multimodal.INGEST_FNS), max_files, stats=stats))


def _unsupported_source_files(source: str) -> list[str]:
    """Files under *source* in a known format with no stdlib parser (ipynb/parquet)."""
    from commontrace.ingest.pipeline import UNSUPPORTED_SUFFIXES

    if os.path.isfile(source):
        return []
    return list(_walk_files(source, UNSUPPORTED_SUFFIXES, None))


def _preview_too_big(fpath: str, result: IngestionResult) -> bool:
    """Count an oversized preview file as skipped_large without reading it."""
    try:
        too_big = os.path.getsize(fpath) > MAX_TEXT_FILE_BYTES
    except OSError:
        return True
    if too_big:
        result.skipped_large += 1
    return too_big


def preview_ingest(
    path: str,
    source_type: str,
    scope: str = "",
    max_files: int = 200,
    **kwargs: Any,
) -> IngestionResult:
    """Parse and count what an ingestion would write, without writing anything."""
    stype = source_type.replace("-", "_")
    result = IngestionResult(source_path=path, source_type=f"{source_type}:preview")
    try:
        if stype == "code":
            walk_stats: dict[str, int] = {}
            for fpath in _walk_files(path, tuple(kwargs.get("extensions", (".py",))), max_files, stats=walk_stats):
                if _preview_too_big(fpath, result):
                    continue
                chunks = _chunk_code_file(fpath)
                result.chunks_extracted += len(chunks)
                result.graph_nodes_written += 1
                for chunk in chunks:
                    if chunk.chunk_type == "code_symbol":
                        result.graph_nodes_written += 1
                        result.graph_edges_written += 1
                    elif chunk.chunk_type == "module_docstring":
                        result.facts_written += 1
            result.truncated += walk_stats.get("truncated", 0)
        elif stype == "markdown":
            walk_stats = {}
            for fpath in _walk_files(path, (".md", ".markdown"), max_files, stats=walk_stats):
                if _preview_too_big(fpath, result):
                    continue
                chunks = _chunk_markdown(fpath)
                result.chunks_extracted += len(chunks)
                result.facts_written += sum(
                    1 for c in chunks if len(f"{c.breadcrumb}: {c.content[:200]}".strip()) > 30)
            result.truncated += walk_stats.get("truncated", 0)
        elif stype == "json_logs":
            buckets = _log_buckets(path, result)
            result.graph_nodes_written = 1 + len(buckets)
            result.graph_edges_written = len(buckets)
            result.traces_written = sum(1 for b in buckets.values() if b["count"] >= 2)
        elif stype == "transcript":
            result.traces_written = min(len(_failure_turns(path, result)), 50)
        elif stype == "fact_triples":
            triples = _load_triples(path)
            result.chunks_extracted = len(triples)
            result.graph_nodes_written = 2 * len(triples)
            result.graph_edges_written = len(triples)
            result.facts_written = len(triples)
        elif stype == "multimodal":
            from commontrace.ingest import multimodal

            walk_stats = {}
            for fpath in _multimodal_targets(path, max_files, stats=walk_stats):
                parsed = multimodal.ingest_multimodal(fpath)
                result.chunks_extracted += parsed.chunks_extracted
                result.errors.extend(parsed.errors)
                result.facts_written += sum(
                    1 for c in getattr(parsed, "chunks", []) or []
                    if len(f"{c.breadcrumb}: {c.content[:200]}".strip()) > 30)
            result.truncated += walk_stats.get("truncated", 0)
            result.skipped_unsupported += len(_unsupported_source_files(path))
        elif stype in ("pipeline", "modular"):
            from commontrace.ingest.pipeline import create_default_pipeline

            report = create_default_pipeline(path, "", scope=scope).preview(limit=None)
            result.chunks_extracted = len(report.chunks)
            result.facts_written = len(report.chunks)
            result.errors.extend(w for w in report.warnings if "Preview limited" not in w)
        else:
            result.errors.append(f"unknown source_type: {source_type!r}")
    except (OSError, ValueError) as exc:
        result.errors.append(f"preview error: {exc}")
    return result


def ingest_multimodal_document(
    root: str,
    source: str,
    scope: str = "",
    max_files: int = 100,
    *,
    force: bool = False,
) -> IngestionResult:
    """PDF, DOCX, HTML, image, audio and subtitle files into reference facts.

    Resumable: files unchanged since the last ingest are skipped via the ledger;
    oversized files, unsupported formats and max_files truncation are counted,
    never silent. PDF rows record extraction quality (chars vs file bytes)."""
    from commontrace import vision
    from commontrace.ingest import multimodal

    result = IngestionResult(source_path=source, source_type="multimodal")
    ledger = _ledger_for(root, force)
    facts: list[dict[str, Any]] = []
    walk_stats: dict[str, int] = {}
    for fpath in _multimodal_targets(source, max_files, stats=walk_stats):
        if _skip_if_unchanged(ledger, fpath, result):
            continue
        if _skip_if_large(fpath, multimodal.MAX_FILE_BYTES, ledger, result):
            result.errors.append(f"{fpath!r} is over the {multimodal.MAX_FILE_BYTES}-byte limit; skipped")
            continue
        parsed = multimodal.ingest_multimodal(fpath)
        result.chunks_extracted += parsed.chunks_extracted
        result.errors.extend(parsed.errors)
        if any("unsupported multimodal extension" in e for e in parsed.errors):
            result.skipped_unsupported += 1
        chunks = list(getattr(parsed, "chunks", []) or [])
        if os.path.splitext(fpath)[1].lower() in multimodal.IMAGE_EXTENSIONS and not parsed.errors:
            caption = vision.describe_image(fpath)
            if caption and vision.vision_enabled():
                chunks.append(Chunk(
                    content=caption,
                    source_path=fpath,
                    chunk_id=f"{_fingerprint(fpath)}_caption",
                    breadcrumb=os.path.basename(fpath),
                    chunk_type="image_caption",
                ))
                result.chunks_extracted += 1
        for chunk in chunks:
            statement = _screened_statement(f"{chunk.breadcrumb}: {chunk.content[:200]}", result)
            if statement:
                facts.append({"statement": statement, "category": "reference",
                              "scopes": [scope] if scope else [], "confidence": 0.6})
        if ledger is not None:
            detail: dict[str, Any] | None = None
            if os.path.splitext(fpath)[1].lower() == ".pdf" and not parsed.errors:
                try:
                    file_bytes = os.path.getsize(fpath)
                except OSError:
                    file_bytes = -1
                detail = {"bytes": file_bytes, "chars": sum(len(c.content) for c in chunks)}
            ledger.note(fpath, "ingested" if not parsed.errors else "error", detail=detail)
    for upath in _unsupported_source_files(source):
        result.skipped_unsupported += 1
        if ledger is not None:
            ledger.note(upath, "skipped_unsupported", detail={"suffix": os.path.splitext(upath)[1].lower()})
    _write_facts(root, facts, result)
    result.truncated += walk_stats.get("truncated", 0)
    if ledger is not None and _facts_ok(result):
        ledger.commit()
    return result


class IngestionPipeline:
    """Dispatch a source to its connector by type."""

    def ingest_source(
        self,
        path: str,
        source_type: str,
        dest_root: str,
        scope: str = "",
        preview: bool = False,
        **kwargs: Any,
    ) -> IngestionResult:
        """Ingest *path* as *source_type* into the store at *dest_root* (or only preview it)."""
        stype = source_type.replace("-", "_")
        if preview:
            return preview_ingest(path, stype, scope=scope, **kwargs)
        if stype == "code":
            return ingest_code_repository(dest_root, path, scope=scope, **kwargs)
        if stype == "markdown":
            return ingest_markdown_documentation(dest_root, path, scope=scope, **kwargs)
        if stype == "json_logs":
            return ingest_json_logs(dest_root, path, scope=scope, **kwargs)
        if stype == "transcript":
            return ingest_failure_transcript(dest_root, path, scope=scope, **kwargs)
        if stype == "fact_triples":
            return ingest_fact_triples(path, dest_root, scope=scope, **kwargs)
        if stype == "multimodal":
            return ingest_multimodal_document(dest_root, path, scope=scope, **kwargs)
        if stype in ("pipeline", "modular"):
            from commontrace.ingest.pipeline import create_default_pipeline

            return create_default_pipeline(path, dest_root, scope=scope, **kwargs).run()
        result = IngestionResult(source_path=path, source_type=source_type)
        result.errors.append(f"unknown source_type: {source_type!r}")
        return result


from commontrace.ingest.pipeline import (  # noqa: E402
    AliasCanonicalizer,
    FileLoader,
    LimitGuard,
    LLMContextualizer,
    Loader,
    MemorySubmitter,
    Pipeline,
    PreviewReport,
    Submitter,
    TextChunker,
    Transform,
    create_default_pipeline,
)

__all__ = [
    "Chunk",
    "IngestionResult",
    "IngestionPipeline",
    "Pipeline",
    "Loader",
    "Transform",
    "Submitter",
    "FileLoader",
    "MemorySubmitter",
    "TextChunker",
    "LLMContextualizer",
    "AliasCanonicalizer",
    "LimitGuard",
    "PreviewReport",
    "create_default_pipeline",
    "ingest_code_repository",
    "ingest_markdown_documentation",
    "ingest_json_logs",
    "ingest_failure_transcript",
    "ingest_fact_triples",
    "ingest_multimodal_document",
    "preview_ingest",
]
