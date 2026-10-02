"""Hierarchical Memory & Atomic Fact Lifecycle engine (EverOS + Mem0 pattern).

Distills noisy agent traces and operational observations into atomic, verifiable facts.
Manages full lifecycle transitions: ADD, UPDATE, SUPERSEDE, DELETE, and NOOP reinforcement,
with bitemporal validity and scoped routing.

Includes entity extraction and linking (ported from Mem0's spaCy-based pipeline).
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

# Entity boosting weight for retrieval (from Mem0 pattern)
ENTITY_BOOST_WEIGHT = 0.5


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
    expires_at: str | None = None
    forgotten: bool = False
    source_traces: list[str] = None  # type: ignore[assignment]
    status: str = "active"  # "active" | "superseded" | "deleted"
    superseded_by: str | None = None
    revision: str = ""
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if self.source_traces is None:
            self.source_traces = []

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FACT_FIELDS = frozenset(AtomicFact.__dataclass_fields__)


def _coerce_fact(data: dict[str, Any]) -> AtomicFact:
    """Build an AtomicFact from a JSONL row, tolerating schema drift.

    Unknown keys are ignored (forward-compat) and rows predating the
    ``expires_at`` / ``forgotten`` fields load with their defaults
    (back-compat). Missing required keys still raise, so corrupt rows are
    skipped by ``load_facts`` as before.
    """
    clean = {k: v for k, v in data.items() if k in _FACT_FIELDS}
    if "forgotten" in clean:
        clean["forgotten"] = bool(clean["forgotten"])
    return AtomicFact(**clean)


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
                fact = _coerce_fact(data)
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


def _normalize_expires_at(expires_at: str | None) -> str | None:
    if expires_at is None:
        return None
    from commontrace import ttl

    try:
        return ttl.parse_expiry(expires_at).isoformat()
    except ValueError as exc:
        raise ValueError(
            f"invalid fact `expires_at` value {expires_at!r}: {exc}"
        ) from exc


def add_fact(
    root: str,
    statement: str,
    category: str = DEFAULT_CATEGORY,
    scopes: list[str] | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    expires_at: str | None = None,
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
    normalized_expires_at = _normalize_expires_at(expires_at)
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
        "expires_at": normalized_expires_at,
        "forgotten": False,
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


_UNSET: Any = object()


def update_fact(
    root: str,
    fact_id: str,
    statement: str | None = None,
    category: str | None = None,
    scopes: list[str] | None = None,
    confidence: float | None = None,
    valid_until: str | None = None,
    expires_at: Any = _UNSET,
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
    if expires_at is not _UNSET:
        # None clears the TTL; a string must parse or this raises ValueError.
        fact.expires_at = _normalize_expires_at(expires_at)

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


def _audit_git(root: str, action: str, fact_id: str) -> None:
    """Best-effort git commit auditing a forget/restore. Never raises."""
    try:
        from commontrace import memory_git

        memory_git.commit_all(root, f"commontrace: fact {action} {fact_id}")
    except Exception:
        pass


def forget_fact(root: str, fact_id: str, undo: bool = False) -> AtomicFact:
    """Hide a fact from default listings (``forgotten=True``), reversibly.

    Forgetting keeps the row (unlike ``delete_fact`` which ends validity);
    ``undo=True`` restores it. Both transitions recompute the revision hash
    and are git-audited best-effort via ``memory_git.commit_all``.
    Raises ``KeyError`` when the fact does not exist.
    """
    facts = load_facts(root)
    if fact_id not in facts:
        raise KeyError(f"Fact '{fact_id}' not found")
    fact = facts[fact_id]
    fact.forgotten = not undo
    fact.updated_at = _now()
    fact.revision = _compute_revision(fact.to_dict())
    save_facts(root, facts)
    _audit_git(root, "restore" if undo else "forget", fact_id)
    return fact


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
    include_forgotten: bool = False,
) -> list[AtomicFact]:
    """List facts matching the given filters and temporal validity.

    When `as_of` is provided, facts valid at that timestamp
    (valid_from <= as_of < valid_until) are included regardless of
    whether their current status is 'superseded' or 'deleted'.

    Forgotten facts (see `forget_fact`) are hidden by default and only
    returned when `include_forgotten` is true, including for point-in-time
    queries -- forgetting is a visibility flag, orthogonal to validity.
    """
    facts = load_facts(root)
    results: list[AtomicFact] = []
    moment = lesson_cache.parse_moment(as_of) if as_of else None

    for fact in facts.values():
        if fact.forgotten and not include_forgotten:
            continue
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
    include_forgotten: bool = False,
) -> list[tuple[AtomicFact, float]]:
    """Search active facts by relevance and confidence."""
    candidates = list_facts(
        root, status="active", scope=scope, category=category, as_of=as_of,
        include_forgotten=include_forgotten,
    )
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


# ---------------------------------------------------------------------------
# Append-only v2 additions: forward-looking Foresight records.
# Existing fact lifecycle above is untouched.
# ---------------------------------------------------------------------------

FORESIGHT_STATUSES = ("open", "confirmed", "refuted", "expired")


@dataclass
class Foresight:
    id: str
    statement: str
    owner: str
    evidence_ids: list[str]
    valid_from: str
    status: str  # "open" | "confirmed" | "refuted" | "expired"
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Foresight:
        return Foresight(
            id=str(data.get("id", "")),
            statement=str(data.get("statement", "")),
            owner=str(data.get("owner", "")),
            evidence_ids=[str(e) for e in (data.get("evidence_ids") or [])],
            valid_from=str(data.get("valid_from", "")),
            status=str(data.get("status", "open")),
            created_at=str(data.get("created_at", "")),
        )


def _foresights_file(root: str) -> str:
    return os.path.join(_facts_dir(root), "foresights.jsonl")


def _foresight_id(statement: str, owner: str) -> str:
    norm = _normalize_statement(statement)
    h = hashlib.sha256(f"foresight|{norm}|{owner.strip().lower()}".encode("utf-8")).hexdigest()[:12]
    return f"foresight-{h}"


def record_foresight(
    root: str,
    statement: str,
    owner: str = "",
    evidence_ids: list[str] | None = None,
    valid_from: str | None = None,
    status: str = "open",
) -> Foresight:
    """Persist a forward-looking foresight (prediction/expectation) record."""
    statement = (statement or "").strip()
    if not statement:
        raise ValueError("Foresight statement cannot be empty")
    if status not in FORESIGHT_STATUSES:
        status = "open"
    now_iso = _now()
    evidence = [str(e) for e in (evidence_ids or []) if str(e).strip()]
    fid = _foresight_id(statement, owner or "")
    # Avoid id collision with an existing different record.
    existing = load_foresights(root)
    if any(f.id == fid and f.statement != statement for f in existing):
        fid = f"{fid}-{int(datetime.now(timezone.utc).timestamp()) % 10000}"
    fs = Foresight(
        id=fid,
        statement=statement,
        owner=(owner or "").strip(),
        evidence_ids=evidence,
        valid_from=valid_from or now_iso,
        status=status,
        created_at=now_iso,
    )
    fpath = _foresights_file(root)
    with open(fpath, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(fs.to_dict()) + "\n")
    return fs


def load_foresights(root: str) -> list[Foresight]:
    """Load all foresight records (in file order)."""
    fpath = _foresights_file(root)
    out: list[Foresight] = []
    if not os.path.exists(fpath):
        return out
    with open(fpath, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Foresight.from_dict(json.loads(line)))
            except Exception:
                continue
    return out


def list_foresights(
    root: str,
    status: str = "",
    owner: str = "",
) -> list[Foresight]:
    """List foresights with optional status/owner filters (newest last)."""
    items = load_foresights(root)
    if status:
        items = [f for f in items if f.status == status]
    if owner:
        items = [f for f in items if f.owner == owner]
    return items


# ---------------------------------------------------------------------------
# Entity Extraction & Linking (ported from Mem0's spaCy-based pipeline)
# Includes sophisticated filtering: generic heads, non-specific adjectives,
# formatting artifacts. Global deduplication via normalized text.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _EntityCandidate:
    entity_type: str
    text: str
    source: str
    start: int
    end: int
    confidence: float
    priority: int


# Words that are too generic to be useful as entity heads
_GENERIC_HEADS = {
    "thing", "stuff", "way", "time", "experience", "situation", "case",
    "fact", "matter", "issue", "idea", "thought", "feeling", "place",
    "area", "part", "kind", "type", "sort", "lot", "bit", "day", "year",
    "week", "month", "moment", "instance", "example", "technique", "method",
    "approach", "process", "step", "tool", "result", "outcome", "goal",
    "task", "item", "topic", "scale", "size", "level", "degree", "amount",
    "number", "style", "look", "color", "colour", "shape", "form", "piece",
    "section", "side", "end", "edge", "surface", "point",
}

# Entity labels emitted by spaCy that are usually safe to treat as named entities
_ACCEPTED_NER_LABELS = {
    "PERSON", "ORG", "GPE", "LOC", "FAC", "PRODUCT", "WORK_OF_ART",
    "EVENT", "NORP", "LAW", "LANGUAGE",
}

_REJECTED_NER_LABELS = {
    "DATE", "TIME", "CARDINAL", "ORDINAL", "QUANTITY", "MONEY", "PERCENT",
}

# Generic role words and title-cased English words that should not become
# single-token named entities just because spaCy tagged them as PROPN
_GENERIC_SINGLE_ENTITY_TERMS = {
    "user", "assistant", "agent", "customer", "client", "person", "people",
    "human", "memory", "message", "conversation", "chat", "session", "system",
    "top",
}

# Modifiers that describe circumstance, not content
_CIRCUMSTANTIAL_MODS = {
    "solo", "individual", "team", "group", "joint", "collaborative", "first",
    "last", "next", "previous", "final", "initial", "main", "side", "top",
}

# Adjectives too vague to make a compound entity specific
_NON_SPECIFIC_ADJ = {
    "many", "few", "several", "some", "any", "all", "most", "more", "less",
    "much", "little", "enough", "various", "numerous", "multiple", "countless",
    "great", "good", "bad", "nice", "terrible", "awful", "awesome", "amazing",
    "wonderful", "horrible", "excellent", "poor", "best", "worst", "fine",
    "okay", "new", "old", "recent", "past", "future", "current", "previous",
    "next", "last", "first", "latest", "early", "late", "former", "modern",
    "ancient", "big", "small", "large", "tiny", "huge", "enormous", "long",
    "short", "tall", "high", "low", "wide", "narrow", "thick", "thin", "deep",
    "shallow", "similar", "different", "same", "other", "another", "such",
    "certain", "important", "main", "major", "minor", "key", "primary", "real",
    "actual", "true", "whole", "entire", "full", "complete", "total", "basic",
    "simple", "interesting", "boring", "exciting", "special", "particular",
    "general", "common", "unique", "rare", "typical", "usual", "normal",
    "regular", "possible", "likely", "potential", "available", "necessary",
    "only", "solo", "individual", "team", "group", "joint", "collaborative",
    "final", "initial", "side",
}

# Generic tail words to strip from compound entities
_GENERIC_ENDINGS = {
    "work", "works", "job", "jobs", "task", "tasks", "stuff", "things",
    "thing", "info", "information", "details", "data", "content", "material",
    "materials", "activities", "activity", "efforts", "effort", "options",
    "option", "choices", "choice", "results", "result", "output", "outputs",
    "products", "product", "items", "item",
}

# Capitalized single words that are too generic to be proper nouns
_GENERIC_CAPS = {
    "works", "items", "things", "stuff", "resources", "options", "tips",
    "ideas", "steps", "ways", "methods", "tools", "features", "benefits",
    "examples", "details", "notes", "instructions", "guidelines",
    "recommendations", "suggestions", "overview", "summary", "conclusion",
    "introduction", "pros", "cons", "advantages", "disadvantages",
}

# Markdown/formatting markers to skip during extraction
_FORMATTING_MARKERS = {"*", "-", "+", "\u2022", "\u2013", "\u2014", "#", "##", "###", "**", "__"}


def _is_sentence_start(tokens: list, idx: int) -> bool:
    """Check if a token is at the start of a sentence or after formatting."""
    if idx == 0:
        return True
    tok = tokens[idx]
    if tok.is_sent_start:
        return True
    prev = tokens[idx - 1].text
    return prev in ".!?:" or prev in _FORMATTING_MARKERS or "\n" in prev


def _strip_generic_ending(toks: list) -> list:
    """Remove generic trailing words from compound token sequences."""
    if len(toks) <= 1:
        return toks
    last = toks[-1].lemma_.lower() if hasattr(toks[-1], "lemma_") else toks[-1].lower()
    return toks[:-1] if last in _GENERIC_ENDINGS and len(toks) > 2 else toks


def _lemmatize_compound(toks: list) -> str:
    """Join compound tokens, lemmatizing nouns."""
    return " ".join(t.lemma_ if t.pos_ == "NOUN" else t.text for t in toks)


def _has_artifacts(txt: str) -> bool:
    """Check for formatting artifacts that indicate non-entity text."""
    return any([
        "**" in txt or "__" in txt or ":*" in txt,
        re.search(r"\s\*\s|\s\*$|^\*\s", txt),
        "  " in txt or "\n" in txt or "\t" in txt,
        len(txt) > 100,
        txt.startswith(("\u2022", "-", "+", "\u2013", "\u2014")),
    ])


def _clean_text(txt: str) -> str:
    txt = re.sub(r"^\*+\s*|\s*\*+$", "", txt.strip())
    txt = re.sub(r"\s*:+$", "", txt)
    txt = re.sub(r"^\d+\s*\.\s*", "", txt)
    return " ".join(txt.split())


def _norm_text(txt: str) -> str:
    """Normalize text for deduplication (lowercase, collapse whitespace)."""
    return " ".join(txt.lower().split())


def _looks_like_technical_identifier(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)+", text))


def _has_internal_cap_or_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text) or any(ch.isupper() for ch in text[1:])


def _looks_like_metric_count_token(tok) -> bool:
    return tok.pos_ == "NUM" and bool(re.fullmatch(r"\d[\d,]*(?:\.\d+)?", tok.text))


def _is_metric_list_context(tokens: list, idx: int) -> bool:
    prev_text = tokens[idx - 1].text if idx > 0 else ""
    next_text = tokens[idx + 1].text if idx + 1 < len(tokens) else ""
    return prev_text in {":", ",", ";"} or next_text in {",", ";"}


def _strip_trailing_metric_counts(span_tokens: list, all_tokens: list) -> list:
    while len(span_tokens) > 1 and _looks_like_metric_count_token(span_tokens[-1]):
        tok = span_tokens[-1]
        if "," not in tok.text and not _is_metric_list_context(all_tokens, tok.i):
            break
        span_tokens = span_tokens[:-1]
    return span_tokens


def _is_list_item_name_token(tokens: list, idx: int) -> bool:
    tok = tokens[idx]
    if not tok.text or tok.text in _FORMATTING_MARKERS or not tok.text[0].isupper():
        return False
    if not any(ch.isalpha() for ch in tok.text) or _is_bad_single_name_token(tok):
        return False
    next_tok = tokens[idx + 1] if idx + 1 < len(tokens) else None
    if not next_tok or not _looks_like_metric_count_token(next_tok):
        return False
    return _is_metric_list_context(tokens, idx) or _is_metric_list_context(tokens, idx + 1)


def _is_name_like_token(tok, tokens: list | None = None, idx: int | None = None) -> bool:
    if not tok.text or tok.text in _FORMATTING_MARKERS:
        return False
    if not tok.text[0].isupper():
        return False
    if not any(ch.isalpha() for ch in tok.text):
        return False
    if _is_bad_single_name_token(tok):
        return False
    if tok.pos_ == "PROPN" or tok.tag_ in {"NNP", "NNPS"}:
        return True
    if tokens is not None and idx is not None and _is_list_item_name_token(tokens, idx):
        return True
    if _has_internal_cap_or_digit(tok.text):
        return True
    return (
        tokens is not None
        and idx is not None
        and tok.pos_ == "NOUN"
        and tok.dep_ not in {"compound", "amod"}
        and not _is_sentence_start(tokens, idx)
    )


def _is_bad_single_name_token(tok) -> bool:
    lower = tok.text.lower()
    return lower in _GENERIC_SINGLE_ENTITY_TERMS or lower in _GENERIC_CAPS or tok.is_stop


def _add_candidate(
    candidates: list[_EntityCandidate],
    entity_type: str,
    text: str,
    source: str,
    start: int,
    end: int,
    confidence: float,
    priority: int,
) -> None:
    cleaned = _clean_text(text)
    if not cleaned or len(cleaned) <= 2 or _has_artifacts(cleaned):
        return
    candidates.append(
        _EntityCandidate(
            entity_type=entity_type,
            text=cleaned,
            source=source,
            start=start,
            end=end,
            confidence=confidence,
            priority=priority,
        )
    )


def _add_ner_candidates(doc, candidates: list[_EntityCandidate]) -> None:
    tokens = list(doc)
    for ent in doc.ents:
        if ent.label_ in _REJECTED_NER_LABELS or ent.label_ not in _ACCEPTED_NER_LABELS:
            continue
        ent_tokens = _strip_trailing_metric_counts(list(ent), tokens)
        if not ent_tokens:
            continue
        if any(tok.pos_ == "CCONJ" and tok.text.lower() == "and" for tok in ent_tokens):
            continue
        if len(ent_tokens) == 1 and _is_bad_single_name_token(ent_tokens[0]):
            continue
        if (
            len(ent_tokens) == 1
            and ent_tokens[0].dep_ in {"compound", "amod"}
            and ent_tokens[0].head.pos_ in {"NOUN", "PROPN"}
        ):
            continue
        _add_candidate(
            candidates,
            "PROPER",
            "".join(tok.text_with_ws for tok in ent_tokens).strip(),
            "spacy_ner",
            ent_tokens[0].i,
            ent_tokens[-1].i + 1,
            0.95,
            0,
        )


def _add_technical_identifier_candidates(tokens: list, candidates: list[_EntityCandidate]) -> None:
    for tok in tokens:
        if _looks_like_technical_identifier(tok.text):
            _add_candidate(
                candidates,
                "IDENTIFIER",
                tok.text,
                "technical_identifier",
                tok.i,
                tok.i + 1,
                0.9,
                1,
            )


def _add_proper_name_candidates(tokens: list, candidates: list[_EntityCandidate]) -> None:
    allowed_inner_connectors = {"of", "the", "for", "at", "in"}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not _is_name_like_token(tok, tokens, i):
            i += 1
            continue

        span_tokens = [tok]
        j = i + 1
        while j < len(tokens):
            current = tokens[j]
            if _is_name_like_token(current, tokens, j):
                span_tokens.append(current)
                j += 1
                continue
            if (
                current.text.lower() in allowed_inner_connectors
                and j + 1 < len(tokens)
                and _is_name_like_token(tokens[j + 1], tokens, j + 1)
            ):
                span_tokens.extend([current, tokens[j + 1]])
                j += 2
                continue
            break

        name_tokens = [
            t
            for t in span_tokens
            if _is_name_like_token(t, tokens, t.i) or (0 <= t.i < len(tokens) and _is_list_item_name_token(tokens, t.i))
        ]
        if len(name_tokens) > 1 or not _is_bad_single_name_token(name_tokens[0]):
            text = "".join(t.text_with_ws for t in span_tokens).strip()
            _add_candidate(candidates, "PROPER", text, "proper_name_span", i, j, 0.8, 2)
        i = max(j, i + 1)


def _add_quoted_candidates(text: str, candidates: list[_EntityCandidate]) -> None:
    for m in re.finditer(r'"([^"]+)"', text):
        if len(m.group(1).strip()) > 2:
            _add_candidate(candidates, "QUOTED", m.group(1).strip(), "quoted", -1, -1, 0.75, 3)
    for m in re.finditer(r"(?:^|[\s\(\[{,;])'([^']+)'(?=[\s\.,;:!?\)\]]|$)", text):
        if len(m.group(1).strip()) > 2:
            _add_candidate(candidates, "QUOTED", m.group(1).strip(), "quoted", -1, -1, 0.75, 3)


def _add_topic_phrase_candidates(doc, candidates: list[_EntityCandidate]) -> None:
    for chunk in doc.noun_chunks:
        chunk_tokens = list(chunk)
        split_indices: list[int] = []
        poss_splits: list[int] = []
        for idx, tok in enumerate(chunk_tokens):
            if tok.dep_ == "case" and tok.text in {"'s", "\u2019s", "'"}:
                split_indices.append(idx)
                poss_splits.append(idx)
            elif tok.pos_ == "PUNCT" and tok.text in {"'", '"', "\u2018", "\u2019", "\u201c", "\u201d"}:
                split_indices.append(idx)

        if split_indices:
            groups: list[list] = []
            prev = 0
            for split_idx in split_indices:
                if split_idx > prev:
                    groups.append(chunk_tokens[prev:split_idx])
                if split_idx in poss_splits:
                    next_split = next((s for s in split_indices if s > split_idx), None)
                    owned = chunk_tokens[split_idx + 1 : next_split if next_split else len(chunk_tokens)]
                    if owned:
                        first_content = next((t for t in owned if t.pos_ not in {"PUNCT", "PART"}), None)
                        if not (first_content and first_content.text and first_content.text[0].isupper()):
                            prev = next_split if next_split else len(chunk_tokens)
                            continue
                prev = split_idx + 1
            if prev < len(chunk_tokens):
                groups.append(chunk_tokens[prev:])
        else:
            groups = [chunk_tokens]

        for group in groups:
            if not group:
                continue
            head = next((t for t in reversed(group) if t.pos_ in {"NOUN", "PROPN"}), None)
            if not head:
                continue
            head_generic = head.lemma_.lower() in _GENERIC_HEADS
            content = [
                t
                for t in group
                if t.pos_ not in {"DET", "PRON", "PUNCT", "PART", "ADP", "SCONJ", "NUM"}
                and (t.pos_ == "ADJ" or not t.is_stop)
            ]
            if not content:
                continue

            compound_toks = [t for t in content if t.dep_ == "compound"]
            adj_toks = [t for t in content if t.pos_ == "ADJ" or t.dep_ == "amod"]
            has_spec_adj = any(t.lemma_.lower() not in _NON_SPECIFIC_ADJ for t in adj_toks)
            if head_generic and not has_spec_adj and not compound_toks:
                continue

            if compound_toks:
                is_circ = any(t.lemma_.lower() in _CIRCUMSTANTIAL_MODS for t in compound_toks)
                if is_circ:
                    val = head.text
                    if len(val) > 2:
                        _add_candidate(
                            candidates,
                            "TOPIC",
                            val,
                            "topic_phrase",
                            head.i,
                            head.i + 1,
                            0.45,
                            4,
                        )
                else:
                    filtered = _strip_generic_ending(
                        [t for t in content if not (t.pos_ == "ADJ" and t.lemma_.lower() in _NON_SPECIFIC_ADJ)]
                    )
                    if filtered:
                        phrase = " ".join(t.text for t in filtered)
                        if len(phrase) > 3 and " " in phrase:
                            _add_candidate(
                                candidates,
                                "TOPIC",
                                phrase,
                                "topic_phrase",
                                filtered[0].i,
                                filtered[-1].i + 1,
                                0.45,
                                4,
                            )
            elif len(content) > 1 and has_spec_adj:
                filtered = _strip_generic_ending(
                    [
                        t
                        for t in content
                        if not ((t.pos_ == "ADJ" or t.dep_ == "amod") and t.lemma_.lower() in _NON_SPECIFIC_ADJ)
                    ]
                )
                if filtered:
                    phrase = " ".join(t.text for t in filtered)
                    if len(phrase) > 3 and " " in phrase:
                        _add_candidate(
                            candidates,
                            "TOPIC",
                            phrase,
                            "topic_phrase",
                            filtered[0].i,
                            filtered[-1].i + 1,
                            0.45,
                            4,
                        )


def _spans_overlap(a: _EntityCandidate, b: _EntityCandidate) -> bool:
    if a.start < 0 or b.start < 0:
        return False
    return a.start < b.end and b.start < a.end


def _resolve_candidates(candidates: list[_EntityCandidate]) -> list[tuple[str, str]]:
    """Deduplicate and resolve overlapping entity candidates.

    Global deduplication via normalized text. Higher priority (lower number)
    and higher confidence win. Overlapping spans are resolved by priority.
    """
    deduped_by_text: dict[str, _EntityCandidate] = {}
    for candidate in candidates:
        key = _norm_text(candidate.text)
        current = deduped_by_text.get(key)
        if current is None or (candidate.priority, -candidate.confidence) < (current.priority, -current.confidence):
            deduped_by_text[key] = candidate

    ordered = sorted(
        deduped_by_text.values(),
        key=lambda c: (c.priority, -c.confidence, -(c.end - c.start), c.start),
    )
    accepted: list[_EntityCandidate] = []
    for candidate in ordered:
        if any(
            _spans_overlap(candidate, existing)
            and not (candidate.entity_type == "TOPIC" and " " in candidate.text and existing.entity_type == "PROPER")
            for existing in accepted
        ):
            continue
        accepted.append(candidate)

    accepted.sort(key=lambda c: (c.start if c.start >= 0 else 10**9, c.end, c.priority))
    return [(candidate.entity_type, candidate.text) for candidate in accepted]


def _extract_entities_from_doc(doc) -> list[tuple[str, str]]:
    """Extract typed entity candidates from a spaCy Doc.

    Args:
        doc: A spaCy Doc object (from nlp(text)).

    Returns:
        Deduplicated list of (entity_type, entity_text) tuples.
        Entity types include PROPER, QUOTED, TOPIC, and IDENTIFIER.
    """
    tokens = list(doc)
    candidates: list[_EntityCandidate] = []
    _add_ner_candidates(doc, candidates)
    _add_technical_identifier_candidates(tokens, candidates)
    _add_proper_name_candidates(tokens, candidates)
    _add_quoted_candidates(doc.text, candidates)
    _add_topic_phrase_candidates(doc, candidates)
    return _resolve_candidates(candidates)


def extract_entities(text: str) -> list[tuple[str, str]]:
    """Extract typed entity candidates from text using spaCy.

    Returns empty list if spaCy is unavailable.

    Args:
        text: Input text to extract entities from.

    Returns:
        List of (entity_type, entity_text) tuples. Entity types are
        PROPER, QUOTED, TOPIC, or IDENTIFIER.
    """
    try:
        from spacy import load as spacy_load
        from spacy.lang.en import English
    except ImportError:
        return []

    try:
        nlp = spacy_load("en_core_web_sm")
    except Exception:
        # Fallback to basic English model if full model not available
        try:
            nlp = English()
        except Exception:
            return []

    if nlp is None:
        return []
    return _extract_entities_from_doc(nlp(text))


def extract_entities_batch(texts: list[str], batch_size: int = 32) -> list[list[tuple[str, str]]]:
    """Extract typed entity candidates from multiple texts using spaCy pipe.

    Returns empty list for each text if spaCy is unavailable.

    Args:
        texts: List of input texts to extract entities from.
        batch_size: Batch size for spaCy pipe processing.

    Returns:
        List of entity lists, one per input text. Each entity list contains
        (entity_type, entity_text) tuples.
    """
    if not texts:
        return []

    try:
        from spacy import load as spacy_load
        from spacy.lang.en import English
    except ImportError:
        return [[] for _ in texts]

    try:
        nlp = spacy_load("en_core_web_sm")
    except Exception:
        try:
            nlp = English()
        except Exception:
            return [[] for _ in texts]

    if nlp is None:
        return [[] for _ in texts]

    return [_extract_entities_from_doc(doc) for doc in nlp.pipe(texts, batch_size=batch_size)]


# ---------------------------------------------------------------------------
# Entity Store with batch processing
# ---------------------------------------------------------------------------

@dataclass
class Entity:
    id: str
    text: str
    entity_type: str
    normalized_text: str
    source_traces: list[str]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _entities_file(root: str) -> str:
    return os.path.join(_facts_dir(root), "entities.jsonl")


def _entity_id(text: str, entity_type: str) -> str:
    norm = _norm_text(text)
    h = hashlib.sha256(f"entity|{entity_type}|{norm}".encode("utf-8")).hexdigest()[:12]
    return f"entity-{h}"


def load_entities(root: str) -> dict[str, Entity]:
    """Load all entities from disk into a dictionary keyed by entity id."""
    fpath = _entities_file(root)
    entities: dict[str, Entity] = {}
    if not os.path.exists(fpath):
        return entities

    with open(fpath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                entity = Entity(**data)
                entities[entity.id] = entity
            except Exception:
                continue
    return entities


def save_entities(root: str, entities: dict[str, Entity]) -> None:
    """Save all entities atomically to disk."""
    fpath = _entities_file(root)
    tmp_path = f"{fpath}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for entity in entities.values():
            f.write(json.dumps(entity.to_dict()) + "\n")
    os.replace(tmp_path, fpath)


def add_entity(
    root: str,
    text: str,
    entity_type: str,
    source_trace_id: str = "",
) -> Entity:
    """Add an entity to the store with global deduplication.

    If an entity with the same normalized text and type already exists,
    it is reinforced (source_traces updated, timestamp refreshed).

    Args:
        root: Memory root directory.
        text: Entity text as extracted.
        entity_type: Entity type (PROPER, QUOTED, TOPIC, IDENTIFIER).
        source_trace_id: Optional trace ID that sourced this entity.

    Returns:
        The Entity object (new or reinforced).
    """
    text = text.strip()
    if not text:
        raise ValueError("Entity text cannot be empty")

    normalized = _norm_text(text)
    entities = load_entities(root)
    now_iso = _now()

    # Check for existing match (same normalized text and type)
    for existing in entities.values():
        if existing.normalized_text == normalized and existing.entity_type == entity_type:
            # Reinforce existing entity
            if source_trace_id and source_trace_id not in existing.source_traces:
                existing.source_traces.append(source_trace_id)
            existing.updated_at = now_iso
            save_entities(root, entities)
            return existing

    # Create new entity
    eid = _entity_id(text, entity_type)
    entity = Entity(
        id=eid,
        text=text,
        entity_type=entity_type,
        normalized_text=normalized,
        source_traces=[source_trace_id] if source_trace_id else [],
        created_at=now_iso,
        updated_at=now_iso,
    )
    entities[eid] = entity
    save_entities(root, entities)
    return entity


def add_entities_batch(
    root: str,
    entities: list[tuple[str, str]],
    source_trace_id: str = "",
) -> list[Entity]:
    """Add multiple entities in batch for efficiency.

    Args:
        root: Memory root directory.
        entities: List of (entity_type, entity_text) tuples.
        source_trace_id: Optional trace ID that sourced these entities.

    Returns:
        List of Entity objects (new or reinforced).
    """
    if not entities:
        return []

    loaded = load_entities(root)
    now_iso = _now()
    results: list[Entity] = []

    for entity_type, text in entities:
        text = text.strip()
        if not text:
            continue

        normalized = _norm_text(text)

        # Check for existing match
        found = False
        for existing in loaded.values():
            if existing.normalized_text == normalized and existing.entity_type == entity_type:
                # Reinforce
                if source_trace_id and source_trace_id not in existing.source_traces:
                    existing.source_traces.append(source_trace_id)
                existing.updated_at = now_iso
                results.append(existing)
                found = True
                break

        if not found:
            eid = _entity_id(text, entity_type)
            entity = Entity(
                id=eid,
                text=text,
                entity_type=entity_type,
                normalized_text=normalized,
                source_traces=[source_trace_id] if source_trace_id else [],
                created_at=now_iso,
                updated_at=now_iso,
            )
            loaded[eid] = entity
            results.append(entity)

    save_entities(root, loaded)
    return results


def list_entities(
    root: str,
    entity_type: str = "",
) -> list[Entity]:
    """List entities with optional type filter."""
    entities = load_entities(root)
    if entity_type:
        return [e for e in entities.values() if e.entity_type == entity_type]
    return list(entities.values())


def extract_and_store_entities(
    root: str,
    text: str,
    source_trace_id: str = "",
) -> list[Entity]:
    """Extract entities from text and store them in the entity store.

    Convenience function combining extract_entities and add_entities_batch.

    Args:
        root: Memory root directory.
        text: Input text to extract entities from.
        source_trace_id: Optional trace ID that sourced this text.

    Returns:
        List of Entity objects stored.
    """
    extracted = extract_entities(text)
    if not extracted:
        return []
    return add_entities_batch(root, extracted, source_trace_id)


def build_entity_index_from_store(
    root: str,
    lessons: list[tuple[str, dict]],
) -> dict[str, list[int]]:
    """Build an entity index from stored entities, compatible with retrieval.rank_lessons.

    This function maps entity tokens to lesson indices using the entity store.
    It's designed to work with the entity_index parameter in retrieval.rank_lessons
    to enable entity-boosted retrieval.

    Args:
        root: Memory root directory.
        lessons: List of (path, frontmatter_dict) tuples from the lesson corpus.

    Returns:
        Dictionary mapping normalized entity tokens to sorted lists of lesson indices.
        Compatible with retrieval.rank_lessons entity_index parameter.
    """
    entities = load_entities(root)
    if not entities:
        return {}

    # Build token -> doc_ids index
    index: dict[str, list[int]] = {}
    word_re = re.compile(r"\w+", re.UNICODE)

    # For each lesson, check which entities are linked via source_traces
    for lesson_idx, (path, fm) in enumerate(lessons):
        lesson_id = os.path.basename(path) if isinstance(path, str) else str(path)

        # Find entities that reference this lesson
        for entity in entities.values():
            if lesson_id in entity.source_traces or path in entity.source_traces:
                # Tokenize the entity text for matching
                tokens = word_re.findall(entity.normalized_text)
                for token in tokens:
                    if len(token) > 2:
                        index.setdefault(token.lower(), []).append(lesson_idx)

    # Sort each posting list
    for token in index:
        index[token] = sorted(set(index[token]))  # Dedupe and sort

    return index
