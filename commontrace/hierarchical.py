"""Hierarchical Memory & Atomic Fact Lifecycle engine (EverOS + Mem0 pattern).

Distills noisy agent traces and operational observations into atomic, verifiable facts.
Manages full lifecycle transitions: ADD, UPDATE, SUPERSEDE, DELETE, and NOOP reinforcement,
with bitemporal validity and scoped routing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from commontrace import lesson_cache, paths

DEFAULT_CATEGORY = "general"
CATEGORIES = (
    "architecture",
    "constraint",
    "preference",
    "bug_pattern",
    "tool_rule",
    "environment",
    "general",
)


@dataclass
class AtomicFact:
    id: str
    statement: str
    category: str
    scopes: list[str]
    confidence: float
    confirmations: int
    valid_from: str
    valid_until: str | None
    source_traces: list[str]
    status: str  # "active" | "superseded" | "deleted"
    superseded_by: str | None
    revision: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _facts_dir(root: str) -> str:
    path = os.path.join(paths.memory_dir(root), "facts")
    os.makedirs(path, exist_ok=True)
    return path


def _facts_file(root: str) -> str:
    return os.path.join(_facts_dir(root), "facts.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_statement(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _fact_id(statement: str, scopes: list[str]) -> str:
    norm = _normalize_statement(statement)
    scope_str = ",".join(sorted(scopes))
    h = hashlib.sha256(f"{norm}|{scope_str}".encode("utf-8")).hexdigest()[:12]
    return f"fact-{h}"


def _compute_revision(fact_dict: dict[str, Any]) -> str:
    payload = json.dumps(
        {k: v for k, v in fact_dict.items() if k != "revision"},
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def load_facts(root: str) -> dict[str, AtomicFact]:
    """Load all facts from disk into a dictionary keyed by fact id."""
    fpath = _facts_file(root)
    facts: dict[str, AtomicFact] = {}
    if not os.path.exists(fpath):
        return facts

    with open(fpath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                fact = AtomicFact(**data)
                facts[fact.id] = fact
            except Exception:
                continue
    return facts


def save_facts(root: str, facts: dict[str, AtomicFact]) -> None:
    """Save all facts atomically to disk."""
    fpath = _facts_file(root)
    tmp_path = f"{fpath}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for fact in facts.values():
            f.write(json.dumps(fact.to_dict()) + "\n")
    os.replace(tmp_path, fpath)


def add_fact(
    root: str,
    statement: str,
    category: str = DEFAULT_CATEGORY,
    scopes: list[str] | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    confidence: float = 0.8,
    source_trace_id: str = "",
) -> tuple[AtomicFact, str]:
    """Add a new atomic fact or reinforce an existing one (NOOP).

    Returns (fact, action) where action is 'ADD' or 'NOOP'.
    """
    statement = statement.strip()
    if not statement:
        raise ValueError("Fact statement cannot be empty")

    scopes = sorted(set(s.strip() for s in (scopes or []) if s.strip()))
    if category not in CATEGORIES:
        category = DEFAULT_CATEGORY

    now_iso = _now()
    valid_from = valid_from or now_iso
    facts = load_facts(root)

    # Check for existing match (exact normalized statement and overlapping scopes)
    norm = _normalize_statement(statement)
    for existing in facts.values():
        if existing.status == "active" and _normalize_statement(existing.statement) == norm:
            if not scopes or any(s in existing.scopes for s in scopes):
                # Reinforce existing fact
                existing.confirmations += 1
                existing.confidence = min(1.0, round(existing.confidence + 0.05, 3))
                if source_trace_id and source_trace_id not in existing.source_traces:
                    existing.source_traces.append(source_trace_id)
                # Expand scopes if new ones provided
                for s in scopes:
                    if s not in existing.scopes:
                        existing.scopes.append(s)
                existing.scopes.sort()
                existing.updated_at = now_iso
                existing.revision = _compute_revision(existing.to_dict())
                save_facts(root, facts)
                return existing, "NOOP"

    fid = _fact_id(statement, scopes)
    if fid in facts:
        # Avoid collisions with deactivated facts
        fid = f"{fid}-{int(datetime.now(timezone.utc).timestamp()) % 10000}"

    fact_dict = {
        "id": fid,
        "statement": statement,
        "category": category,
        "scopes": scopes,
        "confidence": min(1.0, max(0.0, round(confidence, 3))),
        "confirmations": 1,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "source_traces": [source_trace_id] if source_trace_id else [],
        "status": "active",
        "superseded_by": None,
        "revision": "",
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    fact_dict["revision"] = _compute_revision(fact_dict)
    fact = AtomicFact(**fact_dict)
    facts[fact.id] = fact
    save_facts(root, facts)
    return fact, "ADD"


def update_fact(
    root: str,
    fact_id: str,
    statement: str | None = None,
    category: str | None = None,
    scopes: list[str] | None = None,
    confidence: float | None = None,
    valid_until: str | None = None,
) -> AtomicFact:
    """Update an existing active fact."""
    facts = load_facts(root)
    if fact_id not in facts:
        raise KeyError(f"Fact '{fact_id}' not found")

    fact = facts[fact_id]
    if statement is not None and statement.strip():
        fact.statement = statement.strip()
    if category is not None and category in CATEGORIES:
        fact.category = category
    if scopes is not None:
        fact.scopes = sorted(set(s.strip() for s in scopes if s.strip()))
    if confidence is not None:
        fact.confidence = min(1.0, max(0.0, round(confidence, 3)))
    if valid_until is not None:
        fact.valid_until = valid_until

    fact.updated_at = _now()
    fact.revision = _compute_revision(fact.to_dict())
    save_facts(root, facts)
    return fact


def supersede_fact(
    root: str,
    old_fact_id: str,
    new_fact_id_or_statement: str,
    scopes: list[str] | None = None,
    category: str | None = None,
    as_of: str | None = None,
) -> tuple[AtomicFact, AtomicFact]:
    """Supersede an existing fact with a new fact statement or ID."""
    facts = load_facts(root)
    if old_fact_id not in facts:
        raise KeyError(f"Old fact '{old_fact_id}' not found")

    old_fact = facts[old_fact_id]
    now_iso = as_of or _now()

    # Determine if new target is already an existing fact id or a new statement
    if new_fact_id_or_statement in facts:
        new_fact = facts[new_fact_id_or_statement]
        if as_of and not new_fact.valid_from:
            new_fact.valid_from = as_of
    else:
        new_fact, _ = add_fact(
            root=root,
            statement=new_fact_id_or_statement,
            category=category or old_fact.category,
            scopes=scopes if scopes is not None else list(old_fact.scopes),
            valid_from=now_iso,
        )
        facts = load_facts(root)

    # Invalidate old fact
    old_fact = facts[old_fact_id]
    old_fact.status = "superseded"
    old_fact.valid_until = now_iso
    old_fact.superseded_by = new_fact.id
    old_fact.updated_at = _now()
    old_fact.revision = _compute_revision(old_fact.to_dict())
    save_facts(root, facts)

    return old_fact, new_fact


def delete_fact(root: str, fact_id: str) -> bool:
    """Soft-delete an active fact."""
    facts = load_facts(root)
    if fact_id not in facts:
        return False

    fact = facts[fact_id]
    now_iso = _now()
    fact.status = "deleted"
    fact.valid_until = now_iso
    fact.updated_at = now_iso
    fact.revision = _compute_revision(fact.to_dict())
    save_facts(root, facts)
    return True


def list_facts(
    root: str,
    status: str = "active",
    scope: str = "",
    category: str = "",
    as_of: str | None = None,
) -> list[AtomicFact]:
    """List facts matching the given filters and temporal validity.

    When `as_of` is provided, facts valid at that timestamp
    (valid_from <= as_of < valid_until) are included regardless of
    whether their current status is 'superseded' or 'deleted'.
    """
    facts = load_facts(root)
    results: list[AtomicFact] = []
    moment = lesson_cache.parse_moment(as_of) if as_of else None

    for fact in facts.values():
        if not moment and status and fact.status != status:
            continue
        if category and fact.category != category:
            continue
        if scope and fact.scopes and scope not in fact.scopes:
            continue
        if moment:
            # Check temporal validity
            if fact.valid_from:
                try:
                    vf = lesson_cache.parse_moment(fact.valid_from)
                    if vf > moment:
                        continue
                except Exception:
                    pass
            if fact.valid_until:
                try:
                    vu = lesson_cache.parse_moment(fact.valid_until)
                    if vu <= moment:
                        continue
                except Exception:
                    pass
            elif fact.status in ("superseded", "deleted"):
                continue

        results.append(fact)

    return sorted(results, key=lambda f: (f.category, -f.confidence, f.id))


def search_facts(
    root: str,
    query: str,
    scope: str = "",
    category: str = "",
    as_of: str | None = None,
    limit: int = 10,
) -> list[tuple[AtomicFact, float]]:
    """Search active facts by relevance and confidence."""
    candidates = list_facts(root, status="active", scope=scope, category=category, as_of=as_of)
    if not candidates or not query.strip():
        return [(c, c.confidence) for c in candidates[:limit]]

    query_tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
    scored: list[tuple[AtomicFact, float]] = []

    for fact in candidates:
        statement_tokens = set(re.findall(r"[a-z0-9]+", fact.statement.lower()))
        if not statement_tokens:
            continue
        overlap = len(query_tokens & statement_tokens)
        if overlap == 0:
            continue
        # Jaccard + lexical recall
        lex_score = overlap / len(query_tokens | statement_tokens)
        # Combined score with confidence weight
        final_score = round(lex_score * 0.7 + (fact.confidence * 0.3), 4)
        scored.append((fact, final_score))

    scored.sort(key=lambda x: -x[1])
    return scored[:limit]
