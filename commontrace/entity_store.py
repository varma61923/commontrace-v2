"""Lightweight per-store entity-relationship memory plus static/dynamic stability tiers.

Literature grounding (cited, not vendored):

- Entity linking follows mem0's exact-match-then-semantic cascade: an exact
  normalized-name match is tried first (free and deterministic) and only the
  misses fall through to a fuzzy step. The semantic step is replaced here by
  plain token overlap, so linking never calls embeddings, LLMs, or NER models.
- Stability tiers follow supermemory's static/dynamic split: long-lived
  identity facts ("static") are kept apart from fast-changing ones
  ("dynamic"); the ``stability`` field on
  :class:`commontrace.hierarchical.AtomicFact` carries that tier.

Stdlib only. The index is a per-store JSONL file mapping a normalized entity
name to ``{type, memory_ids[] (fact ids + lesson slugs), count, updated_at}``,
written atomically (tmp + rename) like the rest of the codebase.
"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone
from typing import Any

from commontrace import _jsonl, paths

MAX_ENTITIES = 5000
MAX_IDS_PER_ENTITY = 200
ENTITY_BOOST_WEIGHT = 0.5
ENTITY_BOOST_CAP = 0.5

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_QUOTED_RE = re.compile(r'"([^"]{3,100})"|\'([^\']{3,100})\'')
_CAP_PHRASE_RE = re.compile(r"\b([A-Z][a-z0-9]+(?:[ \t]+[A-Z][a-z0-9]+){0,3})\b")
_IDENT_RE = re.compile(
    r"\b((?:[A-Za-z]+[.-])+[A-Za-z0-9_-]+|[a-z0-9]+(?:_[a-z0-9]+)+|[a-z]+[A-Z][A-Za-z0-9]*"
    r"|[A-Z][a-z0-9]*[A-Z][A-Za-z0-9]*)\b"
)

_GENERIC_WORDS = frozenset(
    "the a an i we you he she it they this that these those what when where which who whom whose why how "
    "my our your his her its their and or but not no yes if then than so such only also very just great "
    "sometimes congrats hello thanks please".split()
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_entity(name: str) -> str:
    """Lowercased, whitespace-collapsed, de-quoted form used as the index key."""
    cleaned = (name or "").strip().strip("\"'").strip()
    cleaned = re.sub(r"\s+", " ", cleaned.lower()).strip(".,;:!?")
    return cleaned.strip()


def _tokens(text: str) -> frozenset:
    return frozenset(_TOKEN_RE.findall(text.lower()))


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in spans)


def extract_entities(text: str) -> list[tuple[str, str]]:
    """One shared deterministic regex entity extractor (no models, no spaCy).

    Returns ``(type, text)`` pairs in first-occurrence order, deduplicated by
    normalized name. Types: ``QUOTED`` (quoted strings), ``IDENTIFIER``
    (known-prefix identifiers: dotted paths, snake_case, camelCase, ``x-y``
    codes), ``PROPER`` (capitalized phrases, up to 4 words). Both the fact
    write path and the query path use this function, so linking and lookup
    share one vocabulary.
    """
    if not text or not text.strip():
        return []
    found: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    spans: list[tuple[int, int]] = []

    def _offer(etype: str, value: str, start: int, end: int) -> None:
        norm = normalize_entity(value)
        if not norm or len(norm) <= 2 or norm in seen:
            return
        seen.add(norm)
        found.append((etype, value.strip(), start))
        spans.append((start, end))

    for match in _QUOTED_RE.finditer(text):
        value = match.group(1) if match.group(1) is not None else match.group(2)
        if value and value.strip():
            _offer("QUOTED", value.strip(), match.start(), match.end())
    for match in _IDENT_RE.finditer(text):
        span = (match.start(), match.end())
        if _overlaps(span, spans):
            continue
        value = match.group(1)
        if value.lower() in _GENERIC_WORDS:
            continue
        _offer("IDENTIFIER", value, match.start(), match.end())
    for match in _CAP_PHRASE_RE.finditer(text):
        span = (match.start(), match.end())
        if _overlaps(span, spans):
            continue
        value = match.group(1).strip()
        words = value.split()
        if len(words) == 1 and (value.lower() in _GENERIC_WORDS or len(value) <= 2):
            continue
        if all(w.lower() in _GENERIC_WORDS for w in words):
            continue
        _offer("PROPER", value, match.start(), match.end())
    found.sort(key=lambda item: item[2])
    return [(etype, value) for etype, value, _ in found]


def _entities_file(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "entities", "entities.jsonl")


def load_entities(root: str) -> dict[str, dict[str, Any]]:
    """Every indexed entity keyed by normalized name; unreadable rows are skipped."""
    entities: dict[str, dict[str, Any]] = {}
    for row in _jsonl.read_rows(_entities_file(root)):
        try:
            name = normalize_entity(str(row.get("name", "")))
            memory_ids = [str(m) for m in (row.get("memory_ids") or []) if str(m)]
            if not name or not memory_ids:
                continue
            entities[name] = {
                "name": name,
                "type": str(row.get("type", "PROPER")),
                "memory_ids": list(dict.fromkeys(memory_ids))[:MAX_IDS_PER_ENTITY],
                "count": max(int(row.get("count", 0)), 1),
                "updated_at": str(row.get("updated_at", "") or ""),
            }
        except (ValueError, TypeError, AttributeError):
            continue
    return entities


def save_entities(root: str, entities: dict[str, dict[str, Any]]) -> None:
    """Atomically replace the entity index, evicting lowest-count overflow beyond the cap."""
    if len(entities) > MAX_ENTITIES:
        ordered = sorted(
            entities.values(),
            key=lambda e: (int(e.get("count", 0)), str(e.get("updated_at", "")), str(e.get("name", ""))),
        )
        entities = {e["name"]: e for e in ordered[-MAX_ENTITIES:]}
    rows = [entities[name] for name in sorted(entities)]
    _jsonl.write_rows(_entities_file(root), rows)


def _link_into(entities: dict[str, dict[str, Any]], memory_id: str, text: str) -> list[str]:
    """Fold one memory's entities into *entities*; returns the touched names."""
    touched: list[str] = []
    for etype, value in extract_entities(text):
        name = normalize_entity(value)
        entry = entities.get(name)
        if entry is None:
            entry = {"name": name, "type": etype, "memory_ids": [], "count": 0, "updated_at": _now()}
            entities[name] = entry
        if memory_id not in entry["memory_ids"]:
            entry["memory_ids"].append(memory_id)
            if len(entry["memory_ids"]) > MAX_IDS_PER_ENTITY:
                del entry["memory_ids"][0 : len(entry["memory_ids"]) - MAX_IDS_PER_ENTITY]
            entry["count"] = len(entry["memory_ids"])
            entry["updated_at"] = _now()
        touched.append(name)
    return touched


def link_memory(root: str, memory_id: str, text: str) -> list[str]:
    """Link *memory_id* to every entity extracted from *text* (idempotent per memory)."""
    if not memory_id or not text or not text.strip():
        return []
    path = _entities_file(root)
    with _jsonl.locked(path):
        entities = load_entities(root)
        touched = _link_into(entities, str(memory_id), text)
        if touched:
            save_entities(root, entities)
        return touched


def unlink_memory(root: str, memory_id: str) -> int:
    """Remove *memory_id* from every entity list; drop entities left with none."""
    if not memory_id:
        return 0
    path = _entities_file(root)
    with _jsonl.locked(path):
        entities = load_entities(root)
        if not entities:
            return 0
        touched = 0
        for name in list(entities):
            entry = entities[name]
            if str(memory_id) in entry["memory_ids"]:
                entry["memory_ids"] = [m for m in entry["memory_ids"] if m != str(memory_id)]
                touched += 1
                if not entry["memory_ids"]:
                    del entities[name]
                else:
                    entry["count"] = len(entry["memory_ids"])
                    entry["updated_at"] = _now()
        if touched:
            save_entities(root, entities)
        return touched


def rebuild_entity_index(root: str) -> dict[str, int]:
    """Full deterministic rebuild from facts (active, un-forgotten) plus lesson files.

    Facts are folded in sorted-id order and lessons in sorted-filename order,
    so the same store content always yields the same mapping (``updated_at``
    aside, which is wall-clock). Returns ``{"entities": n, "facts": f,
    "lessons": l}``.
    """
    from commontrace import hierarchical

    entities: dict[str, dict[str, Any]] = {}
    facts = 0
    all_facts = hierarchical.load_facts(root)
    for fact_id in sorted(all_facts):
        fact = all_facts[fact_id]
        if fact.status == "deleted" or fact.forgotten:
            continue
        _link_into(entities, fact.id, fact.statement)
        facts += 1
    lessons = 0
    try:
        names = sorted(os.listdir(os.path.join(paths.memory_dir(root), "lessons")))
    except OSError:
        names = []
    for filename in names:
        if not filename.startswith("lesson_") or not filename.endswith(".md"):
            continue
        slug = filename[len("lesson_") : -len(".md")]
        if not slug:
            continue
        try:
            with open(os.path.join(paths.memory_dir(root), "lessons", filename), encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            continue
        _link_into(entities, slug, f"{slug.replace('-', ' ').replace('_', ' ')}\n{body}")
        lessons += 1
    path = _entities_file(root)
    with _jsonl.locked(path):
        save_entities(root, entities)
    return {"entities": len(entities), "facts": facts, "lessons": lessons}


def confirmed_query_entities(root: str, query: str) -> set[str]:
    """Query entities with an exact normalized match in the index (the free first step)."""
    try:
        entities = load_entities(root)
    except OSError:
        return set()
    if not entities:
        return set()
    return {
        normalize_entity(value) for _, value in extract_entities(query or "") if normalize_entity(value) in entities
    }


def entity_boost_for(
    root: str, query: str, candidate_ids: list, weight: float = ENTITY_BOOST_WEIGHT, cap: float = ENTITY_BOOST_CAP
) -> dict:
    """Linked-memory fan-out boosts for *candidate_ids*, bounded so lexical relevance wins.

    Exact normalized matches contribute similarity 1.0; index entities with
    only token overlap contribute their Jaccard similarity (the token-overlap
    stand-in for mem0's semantic step). Each contribution is
    ``similarity * weight * 1/(1 + log1p(count))`` so oft-mentioned entities
    count less, and each id's total is capped at *cap* (default 0.5: never more
    than half a perfect lexical score). Empty store or no matches -> ``{}``.
    Never calls embeddings or LLMs.
    """
    try:
        entities = load_entities(root)
    except OSError:
        return {}
    if not entities or not query or not query.strip() or not candidate_ids:
        return {}
    allowed = set(candidate_ids)
    if not allowed:
        return {}
    boosts: dict = {}
    for _, value in extract_entities(query):
        q_norm = normalize_entity(value)
        q_toks = _tokens(q_norm)
        if not q_toks:
            continue
        for name, entry in entities.items():
            if name == q_norm:
                similarity = 1.0
            else:
                e_toks = _tokens(name)
                if not e_toks or q_toks.isdisjoint(e_toks):
                    continue
                similarity = len(q_toks & e_toks) / len(q_toks | e_toks)
            count_weight = 1.0 / (1.0 + math.log1p(max(int(entry.get("count", 1)), 1)))
            contribution = similarity * weight * count_weight
            for memory_id in entry.get("memory_ids", []):
                if memory_id in allowed:
                    boosts[memory_id] = min(cap, boosts.get(memory_id, 0.0) + contribution)
    return {mid: round(boost, 4) for mid, boost in boosts.items() if boost > 0}
