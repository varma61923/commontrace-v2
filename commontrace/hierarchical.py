"""Atomic facts: distilled propositions with a governed, bitemporal lifecycle."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from commontrace import _jsonl, lesson_cache, paths

DEFAULT_CATEGORY = "general"
CATEGORIES = (
    "architecture",
    "constraint",
    "preference",
    "bug_pattern",
    "tool_rule",
    "environment",
    "reference",
    "general",
)
MAX_STATEMENT_CHARS = 2000


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
    source_traces: list[str] = field(default_factory=list)
    status: str = "active"
    superseded_by: str | None = None
    revision: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FACT_FIELDS = frozenset(AtomicFact.__dataclass_fields__)


def _coerce_fact(data: dict[str, Any]) -> AtomicFact:
    clean = {k: v for k, v in data.items() if k in _FACT_FIELDS}
    if "forgotten" in clean:
        clean["forgotten"] = bool(clean["forgotten"])
    if clean.get("source_traces") is None:
        clean["source_traces"] = []
    return AtomicFact(**clean)


def _facts_dir(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "facts")


def _facts_file(root: str) -> str:
    return os.path.join(_facts_dir(root), "facts.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_statement(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _clean_scopes(scopes) -> list[str]:
    return sorted({str(s).strip() for s in (scopes or []) if str(s).strip()})


def _fact_id(statement: str, scopes: list[str]) -> str:
    norm = _normalize_statement(statement)
    h = hashlib.sha256(f"{norm}|{','.join(sorted(scopes))}".encode("utf-8")).hexdigest()[:12]
    return f"fact-{h}"


def _free_id(base: str, taken) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _compute_revision(fact_dict: dict[str, Any]) -> str:
    payload = json.dumps({k: v for k, v in fact_dict.items() if k != "revision"}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _stamp(fact: AtomicFact) -> None:
    fact.updated_at = _now()
    fact.revision = _compute_revision(fact.to_dict())


def _moment(value: str | None, label: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return lesson_cache.parse_moment(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid fact `{label}` value {value!r}: {exc}") from exc


def _check_window(valid_from: str | None, valid_until: str | None) -> None:
    if valid_from and valid_until and lesson_cache.parse_moment(valid_until) <= lesson_cache.parse_moment(valid_from):
        raise ValueError(f"fact `valid_until` ({valid_until}) must be after `valid_from` ({valid_from})")


def _normalize_expires_at(expires_at: str | None) -> str | None:
    if expires_at is None:
        return None
    from commontrace import ttl

    try:
        return ttl.parse_expiry(expires_at).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid fact `expires_at` value {expires_at!r}: {exc}") from exc


def load_facts(root: str) -> dict[str, AtomicFact]:
    """Every fact on disk, keyed by id; unreadable rows are skipped."""
    facts: dict[str, AtomicFact] = {}
    for row in _jsonl.read_rows(_facts_file(root)):
        try:
            fact = _coerce_fact(row)
        except TypeError:
            continue
        facts[fact.id] = fact
    return facts


def save_facts(root: str, facts: dict[str, AtomicFact]) -> None:
    """Atomically replace the fact file with *facts*."""
    _jsonl.write_rows(_facts_file(root), (fact.to_dict() for fact in facts.values()))


@contextlib.contextmanager
def mutate_facts(root: str) -> Iterator[dict[str, AtomicFact]]:
    """Load, lock and save the fact file around one read-modify-write."""
    path = _facts_file(root)
    with _jsonl.locked(path):
        facts = load_facts(root)
        yield facts
        save_facts(root, facts)


def _matching_active(facts: dict[str, AtomicFact], statement: str, scopes: list[str]) -> AtomicFact | None:
    norm = _normalize_statement(statement)
    for existing in facts.values():
        if existing.status != "active" or _normalize_statement(existing.statement) != norm:
            continue
        if not scopes or not existing.scopes or any(s in existing.scopes for s in scopes):
            return existing
    return None


def _add_locked(
    facts: dict[str, AtomicFact],
    statement: str,
    category: str,
    scopes: list[str],
    valid_from: str | None,
    valid_until: str | None,
    expires_at: str | None,
    confidence: float,
    source_trace_id: str,
) -> tuple[AtomicFact, str]:
    existing = _matching_active(facts, statement, scopes)
    if existing is not None:
        existing.confirmations += 1
        existing.confidence = min(1.0, round(existing.confidence + 0.05, 3))
        if source_trace_id and source_trace_id not in existing.source_traces:
            existing.source_traces.append(source_trace_id)
        existing.scopes = _clean_scopes([*existing.scopes, *scopes])
        _stamp(existing)
        return existing, "NOOP"

    now_iso = _now()
    fact = AtomicFact(
        id=_free_id(_fact_id(statement, scopes), facts),
        statement=statement,
        category=category,
        scopes=scopes,
        confidence=min(1.0, max(0.0, round(float(confidence), 3))),
        confirmations=1,
        valid_from=valid_from or now_iso,
        valid_until=valid_until,
        expires_at=expires_at,
        source_traces=[source_trace_id] if source_trace_id else [],
        created_at=now_iso,
        updated_at=now_iso,
    )
    fact.revision = _compute_revision(fact.to_dict())
    facts[fact.id] = fact
    return fact, "ADD"


def prepare_fact(statement: str, category: str, valid_from, valid_until, expires_at):
    statement = (statement or "").strip()
    if not statement:
        raise ValueError("Fact statement cannot be empty")
    if len(statement) > MAX_STATEMENT_CHARS:
        raise ValueError(f"Fact statement exceeds {MAX_STATEMENT_CHARS} characters ({len(statement)})")
    if category not in CATEGORIES:
        category = DEFAULT_CATEGORY
    valid_from = _moment(valid_from, "valid_from")
    valid_until = _moment(valid_until, "valid_until")
    _check_window(valid_from, valid_until)
    return statement, category, valid_from, valid_until, _normalize_expires_at(expires_at)


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
    """Add a fact, or reinforce the matching active one. Returns (fact, 'ADD' | 'NOOP')."""
    statement, category, valid_from, valid_until, expires_at = prepare_fact(
        statement, category, valid_from, valid_until, expires_at)
    with mutate_facts(root) as facts:
        return _add_locked(
            facts, statement, category, _clean_scopes(scopes), valid_from, valid_until,
            expires_at, confidence, source_trace_id,
        )


def add_facts(root: str, items: list[dict[str, Any]]) -> list[tuple[AtomicFact, str]]:
    """Add or reinforce many facts in one locked write; bad items raise ValueError."""
    prepared = []
    for item in items:
        statement, category, valid_from, valid_until, expires_at = prepare_fact(
            item.get("statement", ""), item.get("category", DEFAULT_CATEGORY),
            item.get("valid_from"), item.get("valid_until"), item.get("expires_at"))
        prepared.append((statement, category, _clean_scopes(item.get("scopes")), valid_from,
                         valid_until, expires_at, float(item.get("confidence", 0.8)),
                         str(item.get("source_trace_id", "") or "")))
    if not prepared:
        return []
    with mutate_facts(root) as facts:
        return [_add_locked(facts, s, c, sc, vf, vu, ea, conf, src)
                for s, c, sc, vf, vu, ea, conf, src in prepared]


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
    """Change fields of an existing fact."""
    new_until = _moment(valid_until, "valid_until") if valid_until is not None else None
    new_expiry = _normalize_expires_at(expires_at) if expires_at is not _UNSET else None
    if statement is not None and len(statement.strip()) > MAX_STATEMENT_CHARS:
        raise ValueError(f"Fact statement exceeds {MAX_STATEMENT_CHARS} characters")
    with mutate_facts(root) as facts:
        if fact_id not in facts:
            raise KeyError(f"Fact '{fact_id}' not found")
        fact = facts[fact_id]
        if statement is not None and statement.strip():
            fact.statement = statement.strip()
        if category is not None and category in CATEGORIES:
            fact.category = category
        if scopes is not None:
            fact.scopes = _clean_scopes(scopes)
        if confidence is not None:
            fact.confidence = min(1.0, max(0.0, round(float(confidence), 3)))
        if new_until is not None:
            _check_window(fact.valid_from, new_until)
            fact.valid_until = new_until
        if expires_at is not _UNSET:
            fact.expires_at = new_expiry
        _stamp(fact)
        return fact


def supersede_fact(
    root: str,
    old_fact_id: str,
    new_fact_id_or_statement: str,
    scopes: list[str] | None = None,
    category: str | None = None,
    as_of: str | None = None,
) -> tuple[AtomicFact, AtomicFact]:
    """End an active fact's validity and point it at its replacement."""
    when = _moment(as_of, "as_of") or _now()
    with mutate_facts(root) as facts:
        if old_fact_id not in facts:
            raise KeyError(f"Old fact '{old_fact_id}' not found")
        old_fact = facts[old_fact_id]
        if old_fact.status != "active":
            raise ValueError(
                f"fact '{old_fact_id}' is {old_fact.status}, not active; only an active fact can be superseded")
        target = new_fact_id_or_statement
        if target in facts:
            new_fact = facts[target]
            if new_fact.status != "active":
                raise ValueError(f"replacement fact '{target}' is {new_fact.status}, not active")
        else:
            statement, cat, _vf, _vu, _ea = prepare_fact(target, category or old_fact.category, None, None, None)
            new_scopes = _clean_scopes(scopes if scopes is not None else old_fact.scopes)
            same = _matching_active(facts, statement, new_scopes)
            if same is not None and same.id == old_fact.id:
                raise ValueError(
                    f"the replacement restates fact '{old_fact_id}' itself; "
                    "supersede it with a statement that differs")
            new_fact, _action = _add_locked(
                facts, statement, cat, new_scopes, when, None, None, old_fact.confidence, "")
        if new_fact.id == old_fact.id:
            raise ValueError(f"fact '{old_fact_id}' cannot supersede itself")
        old_fact.status = "superseded"
        old_fact.valid_until = when
        old_fact.superseded_by = new_fact.id
        _stamp(old_fact)
        return old_fact, new_fact


def _audit_git(root: str, action: str, fact_id: str) -> None:
    try:
        from commontrace import memory_git

        if memory_git.owns_repo(root):
            memory_git.commit_all(root, f"commontrace: fact {action} {fact_id}")
    except Exception:
        pass


def forget_fact(root: str, fact_id: str, undo: bool = False) -> AtomicFact:
    """Hide a fact from default listings, or restore it with ``undo=True``."""
    with mutate_facts(root) as facts:
        if fact_id not in facts:
            raise KeyError(f"Fact '{fact_id}' not found")
        fact = facts[fact_id]
        fact.forgotten = not undo
        _stamp(fact)
    _audit_git(root, "restore" if undo else "forget", fact_id)
    return fact


def delete_fact(root: str, fact_id: str) -> bool:
    """Soft-delete a fact: its validity ends now and it leaves default listings."""
    with mutate_facts(root) as facts:
        fact = facts.get(fact_id)
        if fact is None:
            return False
        if fact.status == "deleted":
            return True
        fact.status = "deleted"
        fact.valid_until = _now()
        _stamp(fact)
        return True


def retire_source(root: str, source_id: str, keep: set[str]) -> int:
    """End the facts that only *source_id* supports and that it no longer states."""
    if not source_id:
        return 0
    ended = 0
    with mutate_facts(root) as facts:
        now_iso = _now()
        for fact in facts.values():
            if fact.id in keep or fact.status != "active" or source_id not in fact.source_traces:
                continue
            fact.source_traces = [s for s in fact.source_traces if s != source_id]
            if not fact.source_traces:
                fact.status = "deleted"
                fact.valid_until = now_iso
                ended += 1
            _stamp(fact)
    return ended


def _valid_at(fact: AtomicFact, moment: datetime) -> bool:
    try:
        if fact.valid_from and lesson_cache.parse_moment(fact.valid_from) > moment:
            return False
        if fact.valid_until:
            return lesson_cache.parse_moment(fact.valid_until) > moment
    except ValueError:
        return False
    return fact.status not in ("superseded", "deleted")


def list_facts(
    root: str,
    status: str = "active",
    scope: str = "",
    category: str = "",
    as_of: str | None = None,
    include_forgotten: bool = False,
) -> list[AtomicFact]:
    """Facts matching the filters; with `as_of`, the facts valid at that moment."""
    moment = lesson_cache.parse_moment(as_of) if as_of else None
    results: list[AtomicFact] = []
    for fact in load_facts(root).values():
        if fact.forgotten and not include_forgotten:
            continue
        if moment is None and status and fact.status != status:
            continue
        if category and fact.category != category:
            continue
        if scope and fact.scopes and scope not in fact.scopes:
            continue
        if moment is not None and not _valid_at(fact, moment):
            continue
        results.append(fact)
    return sorted(results, key=lambda f: (f.category, -f.confidence, f.id))


_TOKEN_RE = re.compile(r"[a-z0-9]+")

_FACT_TOKENS: dict[tuple[str, str], frozenset] = {}


def _fact_tokens(fact: AtomicFact) -> frozenset:
    """Token set of a fact statement, memoized by ``(id, revision)``.

    `search_facts` re-tokenized every fact on every query (mem0 precomputes
    `text_lemmatized` at write time; same idea, lazy). Statements are
    immutable per revision, so the memo is exact; capped to bound memory.
    """
    key = (fact.id, fact.revision)
    toks = _FACT_TOKENS.get(key)
    if toks is None:
        toks = frozenset(_TOKEN_RE.findall(fact.statement.lower()))
        if len(_FACT_TOKENS) < 4096:
            _FACT_TOKENS[key] = toks
    return toks


def search_facts(
    root: str,
    query: str,
    scope: str = "",
    category: str = "",
    as_of: str | None = None,
    limit: int = 10,
    include_forgotten: bool = False,
) -> list[tuple[AtomicFact, float]]:
    """Active facts ranked by token overlap with *query*, weighted by confidence."""
    candidates = list_facts(
        root, status="active", scope=scope, category=category, as_of=as_of,
        include_forgotten=include_forgotten,
    )
    limit = max(0, int(limit))
    query_tokens = set(_TOKEN_RE.findall(query.lower()))
    if not candidates or not query_tokens:
        ranked = sorted(candidates, key=lambda f: (-f.confidence, f.id))
        return [(c, c.confidence) for c in ranked[:limit]]
    scored: list[tuple[AtomicFact, float]] = []
    for fact in candidates:
        statement_tokens = _fact_tokens(fact)
        overlap = len(query_tokens & statement_tokens)
        if not overlap:
            continue
        lex_score = overlap / len(query_tokens | statement_tokens)
        scored.append((fact, round(lex_score * 0.7 + fact.confidence * 0.3, 4)))
    scored.sort(key=lambda x: (-x[1], x[0].id))
    return scored[:limit]


@dataclass(frozen=True)
class _EntityCandidate:
    entity_type: str
    text: str
    source: str
    start: int
    end: int
    confidence: float
    priority: int


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

_ACCEPTED_NER_LABELS = {
    "PERSON", "ORG", "GPE", "LOC", "FAC", "PRODUCT", "WORK_OF_ART",
    "EVENT", "NORP", "LAW", "LANGUAGE",
}

_REJECTED_NER_LABELS = {
    "DATE", "TIME", "CARDINAL", "ORDINAL", "QUANTITY", "MONEY", "PERCENT",
}

_GENERIC_SINGLE_ENTITY_TERMS = {
    "user", "assistant", "agent", "customer", "client", "person", "people",
    "human", "memory", "message", "conversation", "chat", "session", "system",
    "top",
}

_CIRCUMSTANTIAL_MODS = {
    "solo", "individual", "team", "group", "joint", "collaborative", "first",
    "last", "next", "previous", "final", "initial", "main", "side", "top",
}

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

_GENERIC_ENDINGS = {
    "work", "works", "job", "jobs", "task", "tasks", "stuff", "things",
    "thing", "info", "information", "details", "data", "content", "material",
    "materials", "activities", "activity", "efforts", "effort", "options",
    "option", "choices", "choice", "results", "result", "output", "outputs",
    "products", "product", "items", "item",
}

_GENERIC_CAPS = {
    "works", "items", "things", "stuff", "resources", "options", "tips",
    "ideas", "steps", "ways", "methods", "tools", "features", "benefits",
    "examples", "details", "notes", "instructions", "guidelines",
    "recommendations", "suggestions", "overview", "summary", "conclusion",
    "introduction", "pros", "cons", "advantages", "disadvantages",
}

_FORMATTING_MARKERS = {"*", "-", "+", "\u2022", "\u2013", "\u2014", "#", "##", "###", "**", "__"}


def _is_sentence_start(tokens: list, idx: int) -> bool:
    if idx == 0:
        return True
    tok = tokens[idx]
    if tok.is_sent_start:
        return True
    prev = tokens[idx - 1].text
    return prev in ".!?:" or prev in _FORMATTING_MARKERS or "\n" in prev


def _strip_generic_ending(toks: list) -> list:
    if len(toks) <= 1:
        return toks
    last = toks[-1].lemma_.lower() if hasattr(toks[-1], "lemma_") else toks[-1].lower()
    return toks[:-1] if last in _GENERIC_ENDINGS and len(toks) > 2 else toks


def _has_artifacts(txt: str) -> bool:
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
    tokens = list(doc)
    candidates: list[_EntityCandidate] = []
    _add_ner_candidates(doc, candidates)
    _add_technical_identifier_candidates(tokens, candidates)
    _add_proper_name_candidates(tokens, candidates)
    _add_quoted_candidates(doc.text, candidates)
    _add_topic_phrase_candidates(doc, candidates)
    return _resolve_candidates(candidates)


def _extract_entities_fallback(text: str) -> list[tuple[str, str]]:
    candidates: list[_EntityCandidate] = []
    _add_quoted_candidates(text, candidates)

    for m in re.finditer(
        r"\b([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]+)+|[a-z0-9]+(?:_[a-z0-9]+)+|[a-z]+[A-Z][a-zA-Z0-9]*|[A-Z][a-z0-9]+[A-Z][a-zA-Z0-9]*)\b",
        text,
    ):
        val = m.group(1).strip()
        if len(val) > 2 and val.lower() not in _GENERIC_SINGLE_ENTITY_TERMS:
            _add_candidate(candidates, "IDENTIFIER", val, "tech_id_regex", m.start(), m.end(), 0.8, 1)

    for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", text):
        val = m.group(1).strip()
        words = val.split()
        if len(words) <= 4 and all(w.lower() not in _GENERIC_HEADS for w in words):
            _add_candidate(candidates, "PROPER", val, "proper_phrase_regex", m.start(), m.end(), 0.75, 2)

    return _resolve_candidates(candidates)


_SPACY_NLP = None
_SPACY_INITIALIZED = False


def _get_spacy_nlp():
    global _SPACY_NLP, _SPACY_INITIALIZED
    if _SPACY_INITIALIZED:
        return _SPACY_NLP

    try:
        from spacy import load as spacy_load
        nlp = spacy_load("en_core_web_sm")
        if "ner" in getattr(nlp, "pipe_names", []):
            _SPACY_NLP = nlp
    except Exception:
        _SPACY_NLP = None

    _SPACY_INITIALIZED = True
    return _SPACY_NLP


def extract_entities(text: str) -> list[tuple[str, str]]:
    """Extract typed entity candidates from text using spaCy (or regex fallback)."""
    if not text or not text.strip():
        return []

    nlp = _get_spacy_nlp()
    if nlp is not None:
        try:
            return _extract_entities_from_doc(nlp(text))
        except Exception:
            pass

    return _extract_entities_fallback(text)
