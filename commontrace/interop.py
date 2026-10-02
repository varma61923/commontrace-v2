"""COGX-style portable envelope plus third-party memory dump adapters.

The COGX envelope is one JSON document (``cogx/v1``) carrying the durable
half of a store -- lessons, atomic facts, graph nodes/edges, and working
memory blocks -- so two stores (or two tools) can swap memory without
sharing infrastructure:

.. code-block:: json

    {"format": "cogx/v1", "schema_version": 1,
     "exported_at": "2026-01-01T00:00:00+00:00",
     "records": [{"kind": "lesson", "data": {...}}, ...]}

Record ``kind`` is one of ``lesson`` / ``fact`` / ``graph_node`` /
``graph_edge`` / ``block`` (``trace`` is accepted on read as a
compatibility alias and written back to ``memory/traces/``).

The ``import_mem0_dump`` / ``import_zep_episodes`` / ``import_letta_blocks``
adapters convert third-party dump shapes (already-loaded JSON: a dict or a
list) into envelope-style records, which :func:`apply_store` can then write
into a store. Stdlib only.
"""
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
#: ``trace`` is not emitted by :func:`collect_store` but is accepted on read
#: so older/alien envelopes carrying raw traces still import.
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


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

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
    """Wrap validated records in a ``cogx/v1`` envelope.

    Accepts either a list of ``{"kind", "data"}`` records or an existing
    envelope dict (validated and normalized).
    """
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
    """Return ``"cogx"``, ``"csv"``, or ``"jsonl"`` for an import file.

    Never raises: unreadable or ambiguous files fall back to the extension
    (``.csv`` -> csv, anything else -> jsonl).
    """
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


# ---------------------------------------------------------------------------
# Store <-> records
# ---------------------------------------------------------------------------

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
    """Read a store into envelope-style records.

    *kinds* subsets the record kinds (default: every kind). Unknown kinds
    raise ValueError. Lesson rows honor the *status* / *agent_type* filters.
    """
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


def _apply_lesson(root: str, data: dict[str, Any]) -> bool:
    fm = data.get("frontmatter") if isinstance(data.get("frontmatter"), dict) else None
    body = data.get("body", "")
    if fm is None:
        # Tolerate flat/native lesson rows: everything but kind/body is frontmatter.
        fm = {k: v for k, v in data.items() if k not in ("kind", "body", "slug")}
        if not fm.get("name") and not data.get("slug"):
            return False
    slug = str(data.get("slug") or fm.get("name") or "imported")
    if slug.endswith(".md"):
        slug = slug[:-len(".md")]
    if slug.startswith("lesson_"):
        slug = slug[len("lesson_"):]
    slug = _slugify(slug)
    ldir = paths.lessons_dir(root)
    os.makedirs(ldir, exist_ok=True)
    frontmatter.write(os.path.join(ldir, f"lesson_{slug}.md"), dict(fm), str(body or ""))
    return True


def _apply_fact(root: str, data: dict[str, Any]) -> bool:
    from commontrace import hierarchical

    statement = _as_text(data.get("statement"))
    if not statement:
        return False
    # Full-fidelity payloads (as collect_store emits) merge by id so
    # revisions, confirmations, and validity windows survive the round trip.
    if data.get("id") and data.get("revision"):
        try:
            facts = hierarchical.load_facts(root)
            fact = hierarchical.AtomicFact(
                id=str(data["id"]),
                statement=statement,
                category=str(data.get("category") or hierarchical.DEFAULT_CATEGORY),
                scopes=list(data.get("scopes") or []),
                confidence=float(data.get("confidence", 0.8)),
                confirmations=int(data.get("confirmations", 1)),
                valid_from=str(data.get("valid_from") or _utcnow_iso()),
                valid_until=data.get("valid_until"),
                source_traces=list(data.get("source_traces") or []),
                status=str(data.get("status") or "active"),
                superseded_by=data.get("superseded_by"),
                revision=str(data.get("revision") or ""),
                created_at=str(data.get("created_at") or _utcnow_iso()),
                updated_at=str(data.get("updated_at") or _utcnow_iso()),
            )
            facts[fact.id] = fact
            hierarchical.save_facts(root, facts)
            return True
        except Exception:
            pass
    scopes = data.get("scopes")
    scopes = [str(s) for s in scopes if str(s).strip()] if isinstance(scopes, list) else []
    try:
        confidence = float(data.get("confidence", 0.8))
    except (TypeError, ValueError):
        confidence = 0.8
    try:
        hierarchical.add_fact(
            root=root,
            statement=statement,
            category=str(data.get("category") or hierarchical.DEFAULT_CATEGORY),
            scopes=scopes,
            confidence=min(1.0, max(0.0, confidence)),
            source_trace_id=str(data.get("source_id") or ""),
        )
        return True
    except Exception:
        return False


def _apply_graph_node(root: str, data: dict[str, Any]) -> bool:
    from commontrace import graph as graph_mod

    node_id = str(data.get("id") or "").strip()
    if not node_id:
        return False
    try:
        nodes = graph_mod.load_nodes(root)
        now = _utcnow_iso()
        existing = nodes.get(node_id.strip().lower())
        if existing is not None:
            if data.get("name"):
                existing.name = str(data["name"]).strip()
            if isinstance(data.get("properties"), dict):
                existing.properties.update(data["properties"])
            existing.updated_at = str(data.get("updated_at") or now)
        else:
            nodes[node_id.strip().lower()] = graph_mod.GraphNode(
                id=node_id.strip().lower(),
                entity_type=str(data.get("entity_type") or "concept"),
                name=str(data.get("name") or node_id),
                properties=dict(data.get("properties") or {}),
                created_at=str(data.get("created_at") or now),
                updated_at=str(data.get("updated_at") or now),
            )
        graph_mod.save_nodes(root, nodes)
        return True
    except Exception:
        return False


def _apply_graph_edge(root: str, data: dict[str, Any]) -> bool:
    from commontrace import graph as graph_mod

    source = str(data.get("source") or "").strip()
    target = str(data.get("target") or "").strip()
    if not source or not target:
        return False
    try:
        edges = graph_mod.load_edges(root)
        now = _utcnow_iso()
        for edge in edges:
            if (edge.source == source.strip().lower()
                    and edge.target == target.strip().lower()
                    and edge.relation == str(data.get("relation") or "relates_to")):
                if edge.valid_until is None:
                    try:
                        edge.weight = max(edge.weight, float(data.get("weight", 1.0)))
                    except (TypeError, ValueError):
                        pass
                    if isinstance(data.get("properties"), dict):
                        edge.properties.update(data["properties"])
                    graph_mod.save_edges(root, edges)
                    return True
        try:
            weight = float(data.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        edges.append(graph_mod.GraphEdge(
            source=source.strip().lower(),
            target=target.strip().lower(),
            relation=str(data.get("relation") or "relates_to"),
            weight=round(weight, 3),
            valid_from=str(data.get("valid_from") or now),
            valid_until=data.get("valid_until"),
            properties=dict(data.get("properties") or {}),
            created_at=str(data.get("created_at") or now),
        ))
        graph_mod.save_edges(root, edges)
        # add_edge's auto-vivification aside, make sure endpoints exist.
        graph_mod.load_nodes(root)
        return True
    except Exception:
        return False


def _apply_block(root: str, data: dict[str, Any], actor: str) -> bool:
    from commontrace import memory_blocks

    name = str(data.get("name") or data.get("label") or "").strip()
    content = data.get("content", data.get("value", ""))
    content = content if isinstance(content, str) else _as_text(content)
    if not name:
        return False
    try:
        max_chars = int(data.get("max_chars") or data.get("limit") or 2000)
    except (TypeError, ValueError):
        max_chars = 2000
    max_chars = max(max_chars, len(content), 1)
    metadata = data.get("metadata")
    try:
        memory_blocks.set_block(
            root=root, name=name, content=content, max_chars=max_chars,
            actor=actor, reason="cogx import",
            metadata=dict(metadata) if isinstance(metadata, dict) else None,
        )
        return True
    except Exception:
        return False


def _apply_trace(root: str, data: dict[str, Any], agent_type: str) -> bool:
    """Compatibility path for envelopes carrying raw ``trace`` records."""
    import uuid

    from commontrace import templates
    from commontrace.commands.capture_cmd import _id_suffix

    title = _as_text(data.get("title"))
    context = _as_text(data.get("context", data.get("context_text")))
    solution = _as_text(data.get("solution", data.get("solution_text")))
    if not title or not context or not solution:
        return False
    tags = data.get("tags")
    tags = [str(t) for t in tags if str(t).strip()] if isinstance(tags, list) else []
    trace_id = str(uuid.uuid4())
    slug = _slugify(title)
    tdir = paths.traces_dir(root)
    os.makedirs(tdir, exist_ok=True)
    date = datetime.date.today().isoformat()
    fm = templates.trace_frontmatter(
        trace_id, title, str(data.get("agent_type") or agent_type or "general"),
        tags, str(data.get("profile") or ""), None,
    )
    instance = dict(fm)
    instance["context_text"] = context
    instance["solution_text"] = solution
    frontmatter.write(
        os.path.join(tdir, f"{date}_{slug}_{_id_suffix(trace_id)}.md"),
        fm, templates.trace_body(context, solution),
    )
    return True


def apply_store(root: str, records: list[dict[str, Any]],
                actor: str = "import", agent_type: str = "general") -> dict[str, int]:
    """Write envelope records into a store. Returns per-kind applied counts.

    Unknown kinds and invalid payloads are counted under ``"skipped"``
    rather than raising, so one bad record cannot sink a whole handoff.
    """
    counts = {"lesson": 0, "fact": 0, "graph_node": 0, "graph_edge": 0,
              "block": 0, "trace": 0, "skipped": 0}
    for rec in records or []:
        if not isinstance(rec, dict):
            counts["skipped"] += 1
            continue
        kind, data = rec.get("kind"), rec.get("data")
        if kind not in ACCEPTED_KINDS or not isinstance(data, dict):
            counts["skipped"] += 1
            continue
        try:
            if kind == KIND_LESSON:
                ok = _apply_lesson(root, data)
            elif kind == KIND_FACT:
                ok = _apply_fact(root, data)
            elif kind == KIND_GRAPH_NODE:
                ok = _apply_graph_node(root, data)
            elif kind == KIND_GRAPH_EDGE:
                ok = _apply_graph_edge(root, data)
            elif kind == KIND_BLOCK:
                ok = _apply_block(root, data, actor)
            else:
                ok = _apply_trace(root, data, agent_type)
        except Exception:
            ok = False
        counts[kind if ok else "skipped"] += 1
    return counts


# ---------------------------------------------------------------------------
# Third-party dump adapters (Mem0 / Zep / Letta)
# ---------------------------------------------------------------------------

def _coerce_items(doc: Any, *keys: str) -> list[dict[str, Any]]:
    """Vendor dumps vary: a bare list, or a dict holding the list under one
    of several keys. Return the item dicts, or [] for anything else."""
    if isinstance(doc, list):
        return [d for d in doc if isinstance(d, dict)]
    if isinstance(doc, dict):
        for key in keys:
            items = doc.get(key)
            if isinstance(items, list):
                return [d for d in items if isinstance(d, dict)]
        # A single-item dict shaped like one memory/episode/block.
        return []
    return []


def import_mem0_dump(doc: Any) -> list[dict[str, Any]]:
    """Convert a Mem0 export (``memories``/``results`` list) to fact records.

    Mem0 memories are atomic user/model statements, so they map naturally
    onto CommonTrace atomic facts. Accepts a parsed-JSON dict or list.
    """
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
        out.append(make_record(KIND_FACT, data))
    return out


def _lesson_frontmatter(slug: str, title: str, body: str,
                        agent_type: str, source: str) -> dict[str, Any]:
    description = (title or body.splitlines()[0] if body else source)[:140] or source
    return {
        "name": f"lesson_{slug}",
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
        "status": "active",
    }


def import_zep_episodes(doc: Any, agent_type: str = "general") -> list[dict[str, Any]]:
    """Convert Zep graph episodes (``episodes`` list) to lesson records.

    Episodes are longer narratives than Mem0 memories, so they land as
    lessons (title + body) pending curation.
    """
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

    Accepts a ``blocks`` list of ``{label, value, limit}`` dicts, a bare
    list of ``{name/label, content/value}`` dicts, or a plain
    ``{label: value}`` mapping.
    """
    items = _coerce_items(doc, "blocks", "core_memory", "memory", "data", "items")
    if isinstance(doc, dict) and not items:
        items = [
            {"label": str(k), "value": v}
            for k, v in doc.items()
            if isinstance(v, (str, dict)) and k != "format"
        ]
    out: list[dict[str, Any]] = []
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
