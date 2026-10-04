"""COGX-style portable envelope plus third-party memory dump adapters."""
from __future__ import annotations

import datetime
import glob
import json
import os
import re
from typing import Any

from commontrace import frontmatter, paths

COGX_FORMAT = "cogx/v1"
SCHEMA_VERSION = 1

KIND_LESSON = "lesson"
KIND_FACT = "fact"
KIND_GRAPH_NODE = "graph_node"
KIND_GRAPH_EDGE = "graph_edge"
KIND_BLOCK = "block"
KIND_TRACE = "trace"

RECORD_KINDS = (KIND_LESSON, KIND_FACT, KIND_GRAPH_NODE, KIND_GRAPH_EDGE, KIND_BLOCK)
ACCEPTED_KINDS = RECORD_KINDS + (KIND_TRACE,)

_MAX_TEXT = 20_000
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()[:_MAX_TEXT]
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:_MAX_TEXT]
    except (TypeError, ValueError):
        return str(value)[:_MAX_TEXT]


def _slugify(title: str) -> str:
    slug = _SLUG_RE.sub("-", str(title or "").lower()).strip("-")
    return slug[:60] or "imported"


def make_record(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    """One envelope record. Raises ValueError on a bad kind or data."""
    if kind not in ACCEPTED_KINDS:
        raise ValueError(
            f"unknown record kind {kind!r}; expected one of {', '.join(ACCEPTED_KINDS)}"
        )
    if not isinstance(data, dict):
        raise ValueError(f"record data for kind {kind!r} must be a mapping")
    return {"kind": kind, "data": data}


def build_envelope(records: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    """Wrap validated records in a ``cogx/v1`` envelope."""
    if isinstance(records, dict):
        return loads_cogx(json.dumps(records))
    validated = [make_record(r.get("kind", ""), r.get("data", {})) for r in records]
    return {
        "format": COGX_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "exported_at": _utcnow_iso(),
        "records": validated,
    }


def dumps_cogx(records: list[dict[str, Any]] | dict[str, Any]) -> str:
    """Serialize records (or an envelope) to a COGX JSON document."""
    return json.dumps(build_envelope(records), ensure_ascii=False, indent=2) + "\n"


def loads_cogx(text: str) -> dict[str, Any]:
    """Parse and validate a COGX JSON document. Raises ValueError."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not a COGX document: invalid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError("not a COGX document: top level must be an object")
    if doc.get("format") != COGX_FORMAT:
        raise ValueError(
            f"not a COGX document: format is {doc.get('format')!r}, expected {COGX_FORMAT!r}"
        )
    if doc.get("schema_version") not in (SCHEMA_VERSION, str(SCHEMA_VERSION)):
        raise ValueError(
            f"unsupported COGX schema_version {doc.get('schema_version')!r}"
        )
    records = doc.get("records")
    if not isinstance(records, list):
        raise ValueError("not a COGX document: 'records' must be a list")
    validated = []
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise ValueError(f"COGX record #{i} must be an object")
        validated.append(make_record(rec.get("kind", ""), rec.get("data", {})))
    return {
        "format": COGX_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "exported_at": str(doc.get("exported_at") or _utcnow_iso()),
        "records": validated,
    }


def write_cogx(path: str, records: list[dict[str, Any]] | dict[str, Any]) -> str:
    """Write a COGX envelope to *path*. Returns the normalized path."""
    norm_path = paths.safe_prepare_output_path(path)
    with open(norm_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dumps_cogx(records))
    return norm_path


def read_cogx(path: str) -> dict[str, Any]:
    """Read and validate a COGX envelope file. Raises ValueError/OSError."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        return loads_cogx(fh.read())


def is_cogx_document(obj: object) -> bool:
    """True when *obj* (parsed JSON) looks like a COGX envelope."""
    return (
        isinstance(obj, dict)
        and obj.get("format") == COGX_FORMAT
        and isinstance(obj.get("records"), list)
    )


def detect_file_format(path: str) -> str:
    """Return ``"cogx"``, ``"csv"``, or ``"jsonl"`` for an import file."""
    ext = os.path.splitext(str(path))[1].lower()
    if ext == ".csv":
        return "csv"
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            head = fh.read(65536).strip()
    except OSError:
        return "jsonl"
    if not head.startswith("{"):
        return "jsonl"
    try:
        return "cogx" if is_cogx_document(json.loads(head)) else "jsonl"
    except ValueError:
        return "jsonl"


def _lesson_records(root: str, status: str | None = None,
                     agent_type: str | None = None) -> list[dict[str, Any]]:
    from commontrace.commands._format import read_or_warn

    out: list[dict[str, Any]] = []
    pattern = os.path.join(paths.lessons_dir(root), "lesson_*.md")
    for path in sorted(glob.glob(pattern)):
        if os.path.basename(path) == "lesson_template.md":
            continue
        parsed = read_or_warn(frontmatter.read, path)
        if parsed is None:
            continue
        fm, body = parsed
        if status is not None and str(fm.get("status") or "") != status:
            continue
        if agent_type is not None and str(fm.get("agent_type") or "") != agent_type:
            continue
        stem = os.path.basename(path)[:-len(".md")]
        slug = stem[len("lesson_"):] if stem.startswith("lesson_") else stem
        out.append(make_record(KIND_LESSON, {
            "slug": slug,
            "frontmatter": fm,
            "body": body,
        }))
    return out


def _fact_records(root: str) -> list[dict[str, Any]]:
    from commontrace import hierarchical

    try:
        facts = hierarchical.load_facts(root)
    except Exception:
        return []
    return [make_record(KIND_FACT, f.to_dict()) for f in facts.values()]


def _graph_records(root: str) -> list[dict[str, Any]]:
    from commontrace import graph as graph_mod

    out: list[dict[str, Any]] = []
    try:
        nodes = graph_mod.load_nodes(root)
    except Exception:
        nodes = {}
    try:
        edges = graph_mod.load_edges(root)
    except Exception:
        edges = []
    for node in nodes.values():
        try:
            out.append(make_record(KIND_GRAPH_NODE, node.to_dict()))
        except Exception:
            continue
    for edge in edges:
        try:
            out.append(make_record(KIND_GRAPH_EDGE, edge.to_dict()))
        except Exception:
            continue
    return out


def _block_records(root: str) -> list[dict[str, Any]]:
    from commontrace import memory_blocks

    try:
        blocks = memory_blocks.list_blocks(root)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for block in blocks:
        try:
            out.append(make_record(KIND_BLOCK, {
                "name": block.name,
                "content": block.content,
                "max_chars": block.max_chars,
                "metadata": block.metadata,
            }))
        except Exception:
            continue
    return out


def collect_store(root: str, kinds: list[str] | tuple[str, ...] | None = None,
                   status: str | None = None,
                   agent_type: str | None = None) -> list[dict[str, Any]]:
    """Read a store into envelope-style records."""
    want = tuple(kinds) if kinds is not None else RECORD_KINDS
    for kind in want:
        if kind not in RECORD_KINDS:
            raise ValueError(
                f"unknown collect kind {kind!r}; expected one of {', '.join(RECORD_KINDS)}"
            )
    out: list[dict[str, Any]] = []
    if KIND_LESSON in want:
        out.extend(_lesson_records(root, status=status, agent_type=agent_type))
    if KIND_FACT in want:
        out.extend(_fact_records(root))
    if KIND_GRAPH_NODE in want or KIND_GRAPH_EDGE in want:
        for rec in _graph_records(root):
            if rec["kind"] in want:
                out.append(rec)
    if KIND_BLOCK in want:
        out.extend(_block_records(root))
    return out


def _unsafe(fields: dict[str, Any]) -> bool:
    from commontrace import memory_guard

    return memory_guard.scan_fields({k: v for k, v in fields.items() if isinstance(v, str)}).should_block


def _apply_lesson(root: str, data: dict[str, Any]) -> bool:
    from commontrace import lesson_io

    fm = data.get("frontmatter") if isinstance(data.get("frontmatter"), dict) else None
    body = str(data.get("body", "") or "")
    if fm is None:
        fm = {k: v for k, v in data.items() if k not in ("kind", "body", "slug")}
        if not fm.get("name") and not data.get("slug"):
            return False
    slug = str(data.get("slug") or fm.get("name") or "imported")
    slug = slug[:-len(".md")] if slug.endswith(".md") else slug
    slug = _slugify(slug[len("lesson_"):] if slug.startswith("lesson_") else slug)
    if lesson_io.lesson_path(root, slug) is not None:
        return False
    fm = dict(fm)
    fm["name"] = slug
    if fm.get("status") != "archived":
        fm["status"] = "review"
    if _unsafe({"body": body, **{k: fm.get(k) for k in ("description", "applies_when", "do_not_apply_when")}}):
        return False
    ldir = paths.lessons_dir(root)
    os.makedirs(ldir, exist_ok=True)
    frontmatter.write(os.path.join(ldir, f"lesson_{slug}.md"), fm, body)
    return True


def _apply_fact(root: str, data: dict[str, Any]) -> bool:
    from commontrace import hierarchical

    statement = _as_text(data.get("statement"))
    if not statement or _unsafe({"statement": statement}):
        return False
    if data.get("id") and data.get("revision"):
        row = {k: v for k, v in data.items() if k in hierarchical._FACT_FIELDS}
        row["statement"] = statement
        try:
            fact = hierarchical._coerce_fact(row)
            hierarchical.prepare_fact(fact.statement, fact.category, fact.valid_from, fact.valid_until, None)
        except (TypeError, ValueError):
            return False
        with hierarchical.mutate_facts(root) as facts:
            facts[fact.id] = fact
        return True
    scopes = data.get("scopes")
    scopes = [str(s) for s in scopes if str(s).strip()] if isinstance(scopes, list) else []
    try:
        confidence = min(1.0, max(0.0, float(data.get("confidence", 0.8))))
    except (TypeError, ValueError):
        confidence = 0.8
    try:
        hierarchical.add_fact(
            root=root, statement=statement,
            category=str(data.get("category") or hierarchical.DEFAULT_CATEGORY),
            scopes=scopes, confidence=confidence, source_trace_id=str(data.get("source_id") or ""),
            valid_from=data.get("valid_from"),
            valid_until=data.get("valid_until"),
            expires_at=data.get("expires_at") or data.get("expiration_date"),
        )
    except ValueError:
        return False
    return True


def _apply_graph_node(root: str, data: dict[str, Any]) -> bool:
    from commontrace import graph as graph_mod

    node_id = str(data.get("id") or "").strip().lower()
    if not node_id:
        return False
    with graph_mod.batch(root) as txn:
        existing = txn.nodes.get(node_id)
        if existing is not None:
            graph_mod.add_node(root, node_id, existing.entity_type, name=str(data.get("name") or ""),
                               properties=data.get("properties") if isinstance(data.get("properties"), dict) else None)
            return True
        fields = {k: v for k, v in data.items() if k in graph_mod.GraphNode.__dataclass_fields__}
        fields["id"] = node_id
        if fields.get("entity_type") not in graph_mod.ENTITY_TYPES:
            fields["entity_type"] = "concept"
        fields.setdefault("name", node_id)
        fields["properties"] = dict(fields.get("properties") or {})
        now = _utcnow_iso()
        fields.setdefault("created_at", now)
        fields.setdefault("updated_at", now)
        try:
            txn.nodes[node_id] = graph_mod.GraphNode(**fields)
        except TypeError:
            return False
        txn.nodes_dirty = True
    return True


def _apply_graph_edge(root: str, data: dict[str, Any]) -> bool:
    from commontrace import graph as graph_mod

    source = str(data.get("source") or "").strip()
    target = str(data.get("target") or "").strip()
    if not source or not target:
        return False
    try:
        edge = graph_mod.add_edge(
            root, source, target, str(data.get("relation") or graph_mod.FALLBACK_RELATION),
            weight=data.get("weight", 1.0),
            valid_at=data.get("valid_at") or data.get("valid_from") or None,
            invalid_at=data.get("invalid_at") or data.get("valid_until") or None,
            expired_at=data.get("expired_at") or None,
            properties=data.get("properties") if isinstance(data.get("properties"), dict) else None,
        )
    except ValueError:
        return False
    return edge is not None


def _apply_block(root: str, data: dict[str, Any], actor: str) -> bool:
    from commontrace import memory_blocks

    name = str(data.get("name") or data.get("label") or "").strip()
    content = data.get("content", data.get("value", ""))
    content = content if isinstance(content, str) else _as_text(content)
    if not name or _unsafe({"content": content}):
        return False
    try:
        max_chars = int(data.get("max_chars") or data.get("limit") or memory_blocks.DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        max_chars = memory_blocks.DEFAULT_MAX_CHARS
    max_chars = min(max(max_chars, len(content.strip()), 1), memory_blocks.MAX_QUOTA_CHARS)
    metadata = data.get("metadata")
    try:
        memory_blocks.set_block(
            root=root, name=name, content=content, max_chars=max_chars, actor=actor, reason="cogx import",
            metadata=dict(metadata) if isinstance(metadata, dict) else None,
        )
    except memory_blocks.MemoryBlockError:
        return False
    return True


def _apply_trace(root: str, data: dict[str, Any], agent_type: str) -> bool:
    from commontrace import trace_io

    title = _as_text(data.get("title"))
    context = _as_text(data.get("context", data.get("context_text")))
    solution = _as_text(data.get("solution", data.get("solution_text")))
    if not title or not context or not solution:
        return False
    tags = data.get("tags")
    tags = [str(t) for t in tags if str(t).strip()] if isinstance(tags, list) else []
    try:
        trace_io.write_new(root, title=title, context=context, solution=solution, tags=tags,
                           agent_type=str(data.get("agent_type") or agent_type or "general"))
    except ValueError:
        return False
    return True


def apply_store(root: str, records: list[dict[str, Any]],
                actor: str = "import", agent_type: str = "general") -> dict[str, int]:
    """Write envelope records into a store. Returns per-kind applied counts."""
    from commontrace import graph as graph_mod

    counts = {"lesson": 0, "fact": 0, "graph_node": 0, "graph_edge": 0,
              "block": 0, "trace": 0, "skipped": 0}
    appliers = {
        KIND_LESSON: lambda d: _apply_lesson(root, d),
        KIND_FACT: lambda d: _apply_fact(root, d),
        KIND_GRAPH_NODE: lambda d: _apply_graph_node(root, d),
        KIND_GRAPH_EDGE: lambda d: _apply_graph_edge(root, d),
        KIND_BLOCK: lambda d: _apply_block(root, d, actor),
        KIND_TRACE: lambda d: _apply_trace(root, d, agent_type),
    }
    ordered = sorted(
        (r for r in records or [] if isinstance(r, dict)),
        key=lambda r: 0 if r.get("kind") == KIND_GRAPH_NODE else 1,
    )
    counts["skipped"] += sum(1 for r in records or [] if not isinstance(r, dict))
    with graph_mod.batch(root):
        for rec in ordered:
            kind, data = rec.get("kind"), rec.get("data")
            if kind not in appliers or not isinstance(data, dict):
                counts["skipped"] += 1
                continue
            try:
                ok = appliers[kind](data)
            except (OSError, ValueError, TypeError, frontmatter.FrontmatterError):
                ok = False
            counts[kind if ok else "skipped"] += 1
    return counts


def _coerce_items(doc: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(doc, list):
        return [d for d in doc if isinstance(d, dict)]
    if isinstance(doc, dict):
        for key in keys:
            items = doc.get(key)
            if isinstance(items, list):
                return [d for d in items if isinstance(d, dict)]
        return []
    return []


def import_mem0_dump(doc: Any) -> list[dict[str, Any]]:
    """Convert a Mem0 export (``memories``/``results`` list) to fact records."""
    from commontrace import hierarchical

    items = _coerce_items(doc, "memories", "results", "data", "items", "facts")
    if isinstance(doc, dict) and not items and _as_text(
            doc.get("memory", doc.get("text", doc.get("content")))):
        items = [doc]
    out: list[dict[str, Any]] = []
    for item in items:
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        text = _as_text(item.get("memory", item.get("text",
                             item.get("content", item.get("statement", item.get("fact"))))))
        if not text:
            continue
        user = str(item.get("user_id", item.get("userId", meta.get("user_id", "")))).strip()
        category = str(item.get("category", meta.get("category", "general"))).strip()
        if category not in hierarchical.CATEGORIES:
            category = hierarchical.DEFAULT_CATEGORY
        try:
            confidence = float(item.get("confidence", meta.get("confidence", 0.8)))
        except (TypeError, ValueError):
            confidence = 0.8
        source_id = str(item.get("id", item.get("memory_id", item.get("uuid", "")))).strip()
        created = str(item.get("created_at", item.get("createdAt", meta.get("created_at", "")))).strip()
        data: dict[str, Any] = {
            "statement": text,
            "category": category,
            "scopes": [user] if user else [],
            "confidence": min(1.0, max(0.0, confidence)),
            "source": "mem0",
            "source_id": source_id,
        }
        if created:
            data["created_at"] = created
        # Mem0 TTL + bitemporal fields survive the import instead of flattening.
        for key in ("expiration_date", "expires_at", "valid_from", "valid_until"):
            value = _as_text(item.get(key, meta.get(key, "")))
            if value:
                data[key] = value
        out.append(make_record(KIND_FACT, data))
    return out


def _lesson_frontmatter(slug: str, title: str, body: str,
                        agent_type: str, source: str) -> dict[str, Any]:
    description = (title or body.splitlines()[0] if body else source)[:140] or source
    return {
        "name": slug,
        "description": description,
        "tags": [source],
        "agent_type": agent_type or "general",
        "domain": "other",
        "importance": 3,
        "importance_rationale": f"Imported from {source}; not yet curated.",
        "applies_when": "the imported episode's situation recurs",
        "do_not_apply_when": "the episode context does not match",
        "uses": 0,
        "last_hit": "NEVER",
        "status": "review",
    }


def import_zep_episodes(doc: Any, agent_type: str = "general") -> list[dict[str, Any]]:
    """Convert Zep graph episodes (``episodes`` list) to lesson records."""
    items = _coerce_items(doc, "episodes", "messages", "results", "data", "facts", "items")
    out: list[dict[str, Any]] = []
    for item in items:
        content = _as_text(item.get("content", item.get("text",
                              item.get("fact", item.get("summary", item.get("statement"))))))
        if not content:
            continue
        title = _as_text(item.get("title", item.get("name", item.get("role", "")))) or "Zep episode"
        raw_id = str(item.get("uuid", item.get("id", item.get("episode_id", "")))).strip()
        slug = _slugify(raw_id or title)
        fm = _lesson_frontmatter(slug, title, content, agent_type, "zep")
        if raw_id:
            fm["source_traces"] = [raw_id]
        # Zep bi-temporal fields survive as the lesson validity window instead
        # of flattening to undated text.
        valid_at = _as_text(item.get("valid_at", item.get("validAt", "")))
        invalid_at = _as_text(item.get("invalid_at", item.get("invalidAt", "")))
        if valid_at:
            fm["valid_from"] = valid_at
        if invalid_at:
            fm["valid_until"] = invalid_at
        body = f"# {title}\n\n{content}"
        created = str(item.get("created_at", item.get("createdAt", ""))).strip()
        if created:
            body += f"\n\n<!-- imported from zep episode {raw_id or slug} at {created} -->"
        out.append(make_record(KIND_LESSON, {
            "slug": slug, "frontmatter": fm, "body": body,
        }))
    return out


def import_letta_blocks(doc: Any) -> list[dict[str, Any]]:
    """Convert Letta core-memory blocks to block records.

    Archival passages (``passages``/``archival_memory`` lists) become fact
    records alongside the blocks — previously they were silently dropped while
    only core blocks survived the import.
    """
    out: list[dict[str, Any]] = []
    for item in _coerce_items(doc, "passages", "archival_memory", "archival"):
        text = _as_text(item.get("text", item.get("content", item.get("memory", ""))))
        if not text:
            continue
        created = _as_text(item.get("created_at", item.get("createdAt", "")))
        data: dict[str, Any] = {
            "statement": text[:2000],
            "category": "general",
            "scopes": [],
            "confidence": 0.8,
            "source": "letta",
            "source_id": str(item.get("id", "")),
        }
        if created:
            data["created_at"] = created
        out.append(make_record(KIND_FACT, data))
    items = _coerce_items(doc, "blocks", "core_memory", "memory", "data", "items")
    if isinstance(doc, dict) and not items:
        items = [
            {"label": str(k), "value": v}
            for k, v in doc.items()
            if isinstance(v, (str, dict)) and k != "format"
        ]
    for item in items:
        label = str(item.get("label", item.get("name", item.get("key", "")))).strip()
        value = item.get("value", item.get("content", item.get("text", "")))
        if isinstance(value, dict):
            limit_hint = value.get("limit", value.get("max_chars"))
            value = value.get("value", value.get("content", ""))
            if limit_hint is not None and "limit" not in item and "max_chars" not in item:
                item = dict(item, limit=limit_hint)
        content = value if isinstance(value, str) else _as_text(value)
        if not label or not content.strip():
            continue
        try:
            limit = int(item.get("limit", item.get("max_chars", 2000)))
        except (TypeError, ValueError):
            limit = 2000
        out.append(make_record(KIND_BLOCK, {
            "name": label.strip().lower(),
            "content": content.strip(),
            "max_chars": max(limit, len(content.strip()), 1),
            "metadata": {"source": "letta"},
        }))
    return out
