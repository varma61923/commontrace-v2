"""Atomic facts: distilled propositions with a governed, bitemporal lifecycle."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from commontrace import _jsonl, lesson_cache, paths
from commontrace.fact_evidence import MAX_EVIDENCE, EvidenceError, EvidenceResolver, FactEvidence, claim_revision

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

# Stability tiers follow supermemory's static/dynamic split: "stable" facts are
# long-lived (identity, standing constraints) and "dynamic" facts change often.
# "" is unset: legacy behavior, ranking untouched.
STABILITY_TIERS = ("stable", "dynamic")
STABILITY_VALUES = ("", "stable", "dynamic")


def _normalize_stability(value: object) -> str:
    return value if value in STABILITY_TIERS else ""


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
    stability: str = ""
    evidence: list[FactEvidence] = field(default_factory=list)
    evidence_bound: bool = False
    min_support: int = 1
    evidence_revision: str = ""
    memory_type: str = "general"
    origin: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FACT_FIELDS = frozenset(AtomicFact.__dataclass_fields__)


def _coerce_fact(data: dict[str, Any]) -> AtomicFact:
    """Read current and pre-bitemporal rows without dropping valid legacy facts.

    Fact files are durable user data, so adding fields must be a migration-by-read:
    absent timestamps become a present valid-time instant (recorded time when
    available, otherwise now), while malformed rows are still rejected by the
    caller rather than partially entering the index.
    """
    if not isinstance(data, dict):
        raise TypeError("fact row must be an object")
    statement = str(data.get("statement") or "").strip()
    if not statement:
        raise TypeError("fact statement is required")
    scopes = _clean_scopes(data.get("scopes"))
    recorded_fallback = str(data.get("created_at") or "").strip()
    try:
        valid_from = _moment(data.get("valid_from") or recorded_fallback, "valid_from")
    except ValueError:
        valid_from = None
    valid_from = valid_from or _now()
    try:
        valid_until = _moment(data.get("valid_until"), "valid_until")
    except ValueError:
        raise TypeError("invalid fact validity window") from None
    _check_window(valid_from, valid_until)
    try:
        expires_at = _normalize_expires_at(data.get("expires_at"))
    except ValueError:
        raise TypeError("invalid fact expiry") from None
    confidence = data.get("confidence", 0.8)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError, OverflowError):
        confidence = 0.8
    if confidence != confidence or confidence in (float("inf"), float("-inf")):
        confidence = 0.8
    confirmations = data.get("confirmations", 1)
    try:
        confirmations = max(1, int(confirmations))
    except (TypeError, ValueError, OverflowError):
        confirmations = 1
    forgotten = data.get("forgotten", False)
    if isinstance(forgotten, str):
        forgotten = forgotten.strip().lower() in {"1", "true", "yes", "on"}
    else:
        forgotten = bool(forgotten)
    raw_sources = data.get("source_traces")
    source_values = (
        raw_sources if isinstance(raw_sources, (list, tuple, set))
        else ([raw_sources] if raw_sources else [])
    )
    clean: dict[str, Any] = {
        "id": str(data.get("id") or _fact_id(statement, scopes)),
        "statement": statement,
        "category": str(data.get("category") or DEFAULT_CATEGORY),
        "scopes": scopes,
        "confidence": min(1.0, max(0.0, round(confidence, 3))),
        "confirmations": confirmations,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "expires_at": expires_at,
        "forgotten": forgotten,
        "source_traces": [str(s) for s in source_values if str(s)],
        "status": str(data.get("status") or "active"),
        "superseded_by": data.get("superseded_by"),
        "revision": str(data.get("revision") or ""),
        "created_at": recorded_fallback or valid_from,
        "updated_at": str(data.get("updated_at") or recorded_fallback or valid_from),
        "stability": data.get("stability") if data.get("stability") in STABILITY_VALUES else "",
        "memory_type": str(data.get("memory_type") or "general"),
        "origin": data.get("origin") if isinstance(data.get("origin"), dict) else {},
    }
    raw_evidence = data.get("evidence", [])
    if not isinstance(raw_evidence, list) or len(raw_evidence) > MAX_EVIDENCE:
        raise TypeError("invalid fact evidence ledger")
    receipts = [FactEvidence.from_dict(row) for row in raw_evidence]
    clean["evidence"] = receipts
    bound = data.get("evidence_bound", bool(raw_evidence))
    minimum = data.get("min_support", 1)
    if not isinstance(bound, bool) or not isinstance(minimum, int) or isinstance(minimum, bool) \
            or not 1 <= minimum <= MAX_EVIDENCE:
        raise TypeError("invalid fact evidence policy")
    clean["evidence_bound"], clean["min_support"] = bound, minimum
    clean["evidence_revision"] = str(data.get("evidence_revision") or "")
    if bound:
        clean["confirmations"] = len({(r.kind, r.source_id) for r in receipts if r.polarity == "support"})
    if clean["category"] not in CATEGORIES:
        clean["category"] = DEFAULT_CATEGORY
    fact = AtomicFact(**{k: v for k, v in clean.items() if k in _FACT_FIELDS})
    if not fact.revision:
        fact.revision = _compute_revision(fact.to_dict())
    return fact


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
        except (TypeError, ValueError):
            continue
        facts[fact.id] = fact
    return facts


def save_facts(root: str, facts: dict[str, AtomicFact]) -> None:
    """Atomically replace facts, reusing a verified warm retrieval snapshot.

    JSONL remains authoritative. Incremental publication is an optimization:
    a missing base, external edit or cache failure leaves the coherent cold
    reader available. Exact serialized bytes, not caller-owned mutable facts,
    bind the publication to its committed source generation.
    """
    from commontrace import fact_index

    with _jsonl.locked(_facts_file(root)):
        try:
            base = fact_index.capture_for_write(root)
        except Exception:
            fact_index.clear_cache()
            base = None
        if base is None:
            _jsonl.write_rows(_facts_file(root), (fact.to_dict() for fact in facts.values()))
            try:
                # Withdraw scoped statistics even when an oversized/expired
                # snapshot was not retained. The no-base path never rebuilds.
                fact_index.publish_committed(root, None, (), "")
            except Exception:
                fact_index.clear_cache()
            return
        rows = tuple(json.dumps(fact.to_dict(), ensure_ascii=False) for fact in facts.values())
        digest = _jsonl.write_serialized_rows(_facts_file(root), rows)
        try:
            fact_index.publish_committed(root, base, rows, digest)
        except Exception:
            # A durable successful write must not be reported as failed merely
            # because its optional retrieval acceleration could not publish.
            fact_index.clear_cache()


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
    requested_scopes = frozenset(scopes)
    for existing in facts.values():
        if existing.status != "active" or _normalize_statement(existing.statement) != norm:
            continue
        # Scope is an authorization boundary, not a relevance hint. A scoped
        # write must never reinforce or re-scope a global fact (or another
        # tenant's fact); scoped facts may reinforce when their scope sets
        # overlap, preserving the existing multi-project fact semantics.
        if not requested_scopes and existing.scopes:
            continue
        if requested_scopes and not existing.scopes:
            continue
        if not requested_scopes or requested_scopes & frozenset(existing.scopes):
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
    stability: str = "",
    created_at: str | None = None,
    evidence: Sequence[FactEvidence] | None = None,
    min_support: int = 1,
) -> tuple[AtomicFact, str]:
    existing = _matching_active(facts, statement, scopes)
    if existing is not None:
        if existing.evidence_bound or evidence is not None:
            before = existing.to_dict()
            ledger = {receipt.key: receipt for receipt in existing.evidence}
            for receipt in evidence or ():
                ledger.setdefault(receipt.key, receipt)
            if len(ledger) > MAX_EVIDENCE:
                raise EvidenceError(f"a fact may retain at most {MAX_EVIDENCE} evidence receipts")
            existing.evidence = list(ledger.values())
            existing.evidence_bound = True
            if evidence is not None:
                existing.min_support = min_support
            existing.confirmations = len({(r.kind, r.source_id) for r in existing.evidence if r.polarity == "support"})
            if evidence is not None:
                existing.evidence_revision = claim_revision(existing)
            # Recorded evidence counts are not a model's probability of truth.
            # Replays neither boost confidence nor revise the receipt timestamp.
            if existing.to_dict() != before:
                _stamp(existing)
            return existing, "NOOP"
        if source_trace_id and source_trace_id in existing.source_traces:
            # Replaying the same named trace is not a new confirmation.
            return existing, "NOOP"
        existing.confirmations += 1
        existing.confidence = min(1.0, round(existing.confidence + 0.05, 3))
        if source_trace_id and source_trace_id not in existing.source_traces:
            existing.source_traces.append(source_trace_id)
        existing.scopes = _clean_scopes([*existing.scopes, *scopes])
        if stability in STABILITY_TIERS:
            existing.stability = stability
        _stamp(existing)
        return existing, "NOOP"

    now_iso = _now()
    recorded_at = now_iso
    if created_at:
        try:
            recorded_at = _moment(created_at, "created_at") or now_iso
        except ValueError:
            raise ValueError(f"invalid fact `created_at` value {created_at!r}") from None
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
        created_at=recorded_at,
        updated_at=now_iso,
        stability=_normalize_stability(stability),
        evidence=list(dict((receipt.key, receipt) for receipt in evidence or ()).values()),
        evidence_bound=evidence is not None,
        min_support=min_support,
    )
    if fact.evidence_bound:
        fact.confirmations = len({(r.kind, r.source_id) for r in fact.evidence if r.polarity == "support"})
        fact.evidence_revision = claim_revision(fact)
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
    stability: str = "",
    created_at: str | None = None,
    *,
    evidence: Sequence[FactEvidence] | None = None,
    min_support: int = 1,
) -> tuple[AtomicFact, str]:
    """Add a fact or reinforce a matching active one; return 'ADD' or 'NOOP'.

    Supplying ``evidence`` opts into source-revision admission, whose distinct
    source counts do not increase confidence. Replay of a named legacy trace is
    also idempotent; anonymous legacy reinforcement remains compatible.
    """
    statement, category, valid_from, valid_until, expires_at = prepare_fact(
        statement, category, valid_from, valid_until, expires_at)
    _validate_evidence_policy(min_support)
    with mutate_facts(root) as facts:
        clean_scopes = _clean_scopes(scopes)
        if evidence is not None:
            existing = _matching_active(facts, statement, clean_scopes)
            if existing and any(receipt.kind == "fact" and receipt.source_id == existing.id
                                for receipt in evidence if isinstance(receipt, FactEvidence)):
                raise EvidenceError("a fact cannot support or refute itself")
            EvidenceResolver(root, facts).validate_receipts(evidence, existing.scopes if existing else clean_scopes)
        fact, action = _add_locked(
            facts, statement, category, clean_scopes, valid_from, valid_until,
            expires_at, confidence, source_trace_id, _normalize_stability(stability), created_at,
            evidence, min_support,
        )
    _link_entities_best_effort(root, [(fact.id, fact.statement)])
    return fact, action


def _validate_evidence_policy(min_support: int) -> None:
    if isinstance(min_support, bool) or not isinstance(min_support, int) or not 1 <= min_support <= MAX_EVIDENCE:
        raise EvidenceError(f"min_support must be an integer in 1..{MAX_EVIDENCE}")


def add_facts(root: str, items: list[dict[str, Any]]) -> list[tuple[AtomicFact, str]]:
    """Add or reinforce many facts in one locked write; bad items raise ValueError."""
    prepared = []
    for item in items:
        statement, category, valid_from, valid_until, expires_at = prepare_fact(
            item.get("statement", ""), item.get("category", DEFAULT_CATEGORY),
            item.get("valid_from"), item.get("valid_until"), item.get("expires_at"))
        prepared.append((statement, category, _clean_scopes(item.get("scopes")), valid_from,
                         valid_until, expires_at, float(item.get("confidence", 0.8)),
                         str(item.get("source_trace_id", "") or ""),
                         _normalize_stability(item.get("stability", "")),
                         item.get("created_at"), item.get("evidence"), item.get("min_support", 1)))
    if not prepared:
        return []
    with mutate_facts(root) as facts:
        results = []
        for s, c, sc, vf, vu, ea, conf, src, stab, created, receipts, minimum in prepared:
            _validate_evidence_policy(minimum)
            if receipts is not None:
                if not isinstance(receipts, (list, tuple)):
                    raise EvidenceError("evidence must be a sequence of FactEvidence receipts")
                existing = _matching_active(facts, s, sc)
                if existing and any(receipt.kind == "fact" and receipt.source_id == existing.id
                                    for receipt in receipts if isinstance(receipt, FactEvidence)):
                    raise EvidenceError("a fact cannot support or refute itself")
                EvidenceResolver(root, facts).validate_receipts(receipts, existing.scopes if existing else sc)
            results.append(_add_locked(facts, s, c, sc, vf, vu, ea, conf, src, stab, created, receipts, minimum))
    _link_entities_best_effort(root, [(fact.id, fact.statement) for fact, _ in results])
    return results


def append_facts(root: str, items: list[dict[str, Any]]) -> list[tuple[AtomicFact, str]]:
    """Strict ADD-only admission: replay never changes an existing fact.

    Contrary statements coexist with their valid/recorded times. Superseding,
    reinforcing or forgetting remains an explicit governed operation. All
    items are validated before the batch can write anything.
    """
    import math

    from commontrace import injection_guard, memory_guard

    if not isinstance(items, list) or len(items) > 200:
        raise ValueError("append batch must be a list of at most 200 facts")
    prepared = []
    for item in items:
        if not isinstance(item, dict) or set(item) - {
                "statement", "category", "scopes", "valid_from", "valid_until", "expires_at",
                "confidence", "source_trace_id", "stability", "memory_type"}:
            raise ValueError("unsupported ADD-only fact fields")
        from commontrace.decay import HALF_LIVES_DAYS
        from commontrace.ttl import expiry_for_type

        memory_type = item.get("memory_type", "general")
        if memory_type not in HALF_LIVES_DAYS:
            raise ValueError("unknown memory type")
        expiry_input = item.get("expires_at") or expiry_for_type(memory_type, valid_from=item.get("valid_from"))
        statement, category, start, end, expiry = prepare_fact(
            item.get("statement", ""), item.get("category", DEFAULT_CATEGORY),
            item.get("valid_from"), item.get("valid_until"), expiry_input)
        statement = memory_guard.sanitize_metadata(
            {"statement": statement}, pii=memory_guard.privacy_redaction_enabled())[0]["statement"]
        if injection_guard.injection_labels({"text": statement}):
            raise ValueError("fact failed the injection screen")
        confidence = float(item.get("confidence", 0.8))
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be finite in [0, 1]")
        labels = item.get("scopes", [])
        if not isinstance(labels, list) or any(not isinstance(s, str) for s in labels):
            raise ValueError("scopes must be a list of strings")
        prepared.append((statement, category, _clean_scopes(labels), start, end, expiry,
                         confidence, str(item.get("source_trace_id", "")), item.get("stability", ""), memory_type))
    results = []
    with mutate_facts(root) as facts:
        for statement, category, labels, start, end, expiry, confidence, source, stability, memory_type in prepared:
            duplicate = next((f for f in facts.values() if f.status == "active" and not f.forgotten
                              and _normalize_statement(f.statement) == _normalize_statement(statement)
                              and f.scopes == labels and (start is None or f.valid_from == start)
                              and f.valid_until == end and f.expires_at == expiry), None)
            if duplicate:
                results.append((duplicate, "NOOP"))
                continue
            # _add_locked reinforces overlapping scope sets. Use a fresh map
            # to construct only the new record, then allocate an unused ID.
            fact, action = _add_locked({}, statement, category, labels, start, end, expiry,
                                       confidence, source, stability)
            fact.id = _free_id(fact.id, facts)
            fact.memory_type = memory_type
            from commontrace import memory_authority

            fact.origin = memory_authority.bind(root, memory_authority.fact_record(fact), sources=fact.source_traces)
            fact.revision = _compute_revision(fact.to_dict())
            facts[fact.id] = fact
            results.append((fact, action))
    _link_entities_best_effort(root, [(fact.id, fact.statement) for fact, action in results if action == "ADD"])
    return results


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
    stability: str | None = None,
) -> AtomicFact:
    """Change fields of an existing fact.

    Scopes are immutable here (mem0 tenant-isolation discipline): a scope
    identifies *whose* fact this is, and silently re-scoping it would move one
    tenant's memory into another's view. To change scope, supersede the fact
    with explicit new scopes instead — the old scope stays on record.
    """
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
        if scopes is not None and _clean_scopes(scopes) != list(fact.scopes):
            raise ValueError(
                f"fact '{fact_id}' scopes are immutable via update "
                f"(currently {list(fact.scopes)}); supersede the fact with explicit "
                "scopes to move it, so the old scope stays on record")
        if confidence is not None:
            fact.confidence = min(1.0, max(0.0, round(float(confidence), 3)))
        if new_until is not None:
            _check_window(fact.valid_from, new_until)
            fact.valid_until = new_until
        if expires_at is not _UNSET:
            fact.expires_at = new_expiry
        if stability is not None:
            if stability not in STABILITY_VALUES:
                raise ValueError(f"unknown stability tier {stability!r} (expected 'stable' or 'dynamic')")
            fact.stability = stability
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
    with mutate_facts(root) as facts:
        return _supersede_locked(facts, old_fact_id, new_fact_id_or_statement,
                                 scopes=scopes, category=category, as_of=as_of)


def _supersede_locked(
    facts: dict[str, AtomicFact], old_fact_id: str, new_fact_id_or_statement: str, *,
    scopes: list[str] | None = None, category: str | None = None,
    as_of: str | None = None, contradiction: bool = False,
) -> tuple[AtomicFact, AtomicFact]:
    """Validate and close the old window under the same fact transaction."""
    explicit = _moment(as_of, "as_of")
    when = explicit or _now()
    if old_fact_id not in facts:
        raise KeyError(f"Old fact '{old_fact_id}' not found")
    old_fact = facts[old_fact_id]
    if old_fact.status != "active":
        raise ValueError(
            f"fact '{old_fact_id}' is {old_fact.status}, not active; only an active fact can be superseded")
    target = new_fact_id_or_statement
    new_fact: AtomicFact | None
    if target in facts:
        new_fact = facts[target]
        if new_fact.status != "active":
            raise ValueError(f"replacement fact '{target}' is {new_fact.status}, not active")
        when = explicit or new_fact.valid_from
    else:
        statement, cat, _vf, _vu, _ea = prepare_fact(target, category or old_fact.category, None, None, None)
        new_scopes = _clean_scopes(scopes if scopes is not None else old_fact.scopes)
        same = _matching_active(facts, statement, new_scopes)
        if same is not None and same.id == old_fact.id:
            raise ValueError(f"the replacement restates fact '{old_fact_id}' itself; use a different statement")
        # The prospective validity must be checked before inserting a replacement.
        new_fact = same
        when = explicit or (same.valid_from if same is not None else when)
    if new_fact is not None and new_fact.id == old_fact.id:
        raise ValueError(f"fact '{old_fact_id}' cannot supersede itself")
    if contradiction:
        old_from = lesson_cache.parse_moment(old_fact.valid_from)
        new_from = lesson_cache.parse_moment(when)
        if new_from < old_from:
            raise ValueError("replacement predates the contradicted fact")
        if old_fact.valid_until and lesson_cache.parse_moment(old_fact.valid_until) <= new_from:
            raise ValueError("These cover different windows, not a contradiction.")
        if new_fact is not None and new_fact.valid_until \
                and lesson_cache.parse_moment(new_fact.valid_until) <= old_from:
            raise ValueError("These cover different windows, not a contradiction.")
        if new_fact is not None and new_fact.scopes != old_fact.scopes:
            raise ValueError("contradictory facts must have identical scopes")
        if new_fact is None and new_scopes != old_fact.scopes:
            raise ValueError("contradictory facts must have identical scopes")
    if new_fact is None:
        new_fact, _action = _add_locked(
            facts, statement, cat, new_scopes, when, None, None, old_fact.confidence, "",
            evidence=[] if old_fact.evidence_bound else None, min_support=old_fact.min_support,
        )
    old_fact.status = "superseded"
    old_fact.valid_until = when
    old_fact.superseded_by = new_fact.id
    _stamp(old_fact)
    return old_fact, new_fact


def _link_entities_best_effort(root: str, pairs: list[tuple[str, str]]) -> None:
    """Fold fact statements into the entity index; failures never break the write."""
    try:
        from commontrace import entity_store

        for memory_id, text in pairs:
            try:
                entity_store.link_memory(root, memory_id, text)
            except Exception:
                pass
    except Exception:
        pass


def _unlink_entities_best_effort(root: str, memory_id: str) -> None:
    """Drop a fact id from the entity index; failures never break the write."""
    try:
        from commontrace import entity_store

        try:
            entity_store.unlink_memory(root, memory_id)
        except Exception:
            pass
    except Exception:
        pass


def _fact_line(fact: AtomicFact) -> str:
    scope_str = f" [{','.join(fact.scopes)}]" if fact.scopes else ""
    return f"- {fact.statement} (conf: {fact.confidence:.2f}){scope_str}"


def format_fact_lines(scored: list[tuple[AtomicFact, float]], group_stability: bool = False) -> list[str]:
    """Render ``search_facts`` pairs as injection prompt lines.

    This is the retrieval prompt builder both fact consumers share: the agent
    loop's "# Key facts" block and MCP ``query_facts`` output both start from
    ``search_facts`` pairs. With ``group_stability=False`` (default) the lines
    are byte-identical to the legacy flat rendering; opt in with
    ``group_stability=True`` to group supermemory-style static facts under a
    "## Stable" header and everything else (dynamic + untiered) under
    "## Recent". Input order is kept within each group.
    """
    if not group_stability:
        return [_fact_line(fact) for fact, _score in scored]
    stable = [fact for fact, _score in scored if getattr(fact, "stability", "") == "stable"]
    recent = [fact for fact, _score in scored if getattr(fact, "stability", "") != "stable"]
    lines: list[str] = []
    if stable:
        lines.append("## Stable")
        lines.extend(_fact_line(fact) for fact in stable)
    if recent:
        lines.append("## Recent")
        lines.extend(_fact_line(fact) for fact in recent)
    return lines


def _audit_git(root: str, action: str, fact_id: str) -> None:
    try:
        from commontrace import memory_git

        if memory_git.owns_repo(root):
            memory_git.commit_all(root, f"commontrace: fact {action} {fact_id}")
    except Exception:
        pass


def resolve_contradiction(
    root: str,
    old_fact_id: str,
    new_fact_id_or_statement: str,
    *,
    as_of: str | None = None,
    category: str | None = None,
    scopes: list[str] | None = None,
) -> tuple[AtomicFact, AtomicFact]:
    """Resolve a contradiction by invalidating the older fact in favor of newer evidence.

    Graphiti's deterministic temporal guard, without the LLM: the replacement
    must not be *older* than the fact it invalidates (compared on
    ``valid_from``) and their validity windows must overlap — otherwise this
    refuses instead of expiring a fact that was true in a different window.
    Guard validation and both state changes share one locked transaction.
    """
    with mutate_facts(root) as facts:
        return _supersede_locked(
            facts, old_fact_id, new_fact_id_or_statement, scopes=scopes,
            category=category, as_of=as_of, contradiction=True,
        )


def forget_fact(root: str, fact_id: str, undo: bool = False) -> AtomicFact:
    """Hide a fact from default listings, or restore it with ``undo=True``."""
    with mutate_facts(root) as facts:
        if fact_id not in facts:
            raise KeyError(f"Fact '{fact_id}' not found")
        fact = facts[fact_id]
        from commontrace import memory_authority

        if not undo:
            memory_authority.record_forgetting(root, fact_id, forgotten=True)
        fact.forgotten = not undo
        _stamp(fact)
    if undo:
        memory_authority.record_forgetting(root, fact_id, forgotten=False)
    if undo:
        _link_entities_best_effort(root, [(fact.id, fact.statement)])
    else:
        _unlink_entities_best_effort(root, fact.id)
    _audit_git(root, "restore" if undo else "forget", fact_id)
    return fact


def invalidate_fact(root: str, fact_id: str, at: str | None = None) -> AtomicFact:
    """End an active fact's validity at *at* (default now) without a replacement.

    The fact is kept, not deleted: a query ``as_of`` a moment inside its window
    still returns it, which is what makes "what was true in March" answerable.
    An end in the future only schedules the close; the fact stays active until then.
    """
    end = _moment(at, "at") or _now()
    with mutate_facts(root) as facts:
        if fact_id not in facts:
            raise KeyError(f"Fact '{fact_id}' not found")
        fact = facts[fact_id]
        if fact.status != "active":
            raise ValueError(f"fact '{fact_id}' is {fact.status}, not active; only an active fact can be invalidated")
        _check_window(fact.valid_from, end)
        fact.valid_until = end
        ended = lesson_cache.parse_moment(end) <= datetime.now(timezone.utc)
        if ended:
            fact.status = "invalidated"
        _stamp(fact)
    if ended:
        _unlink_entities_best_effort(root, fact_id)
    _audit_git(root, "invalidate", fact_id)
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
    _unlink_entities_best_effort(root, fact_id)
    return True


def retire_source(root: str, source_id: str, keep: set[str]) -> int:
    """End the facts that only *source_id* supports and that it no longer states."""
    if not source_id:
        return 0
    ended = 0
    ended_ids: list[str] = []
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
                ended_ids.append(fact.id)
            _stamp(fact)
    for fact_id in ended_ids:
        _unlink_entities_best_effort(root, fact_id)
    return ended


def _valid_at(fact: AtomicFact, moment: datetime) -> bool:
    """Whether a fact was true at valid-time *moment*, including past revisions."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    try:
        valid_from = lesson_cache.parse_moment(fact.valid_from) if fact.valid_from else None
        valid_until = lesson_cache.parse_moment(fact.valid_until) if fact.valid_until else None
    except ValueError:
        return False
    if valid_from is not None and valid_from > moment:
        return False
    if valid_until is not None and valid_until <= moment:
        return False
    # Superseded/deleted rows remain queryable through their historical window;
    # status only controls the default present-time listing.
    return True


def _is_expired(fact: AtomicFact, moment: datetime) -> bool:
    """True when the fact's TTL has passed at *moment* (mem0 hide-expired semantics)."""
    if not fact.expires_at:
        return False
    try:
        expiry = lesson_cache.parse_moment(fact.expires_at)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    return expiry <= moment


def list_facts(
    root: str,
    status: str = "active",
    scope: str = "",
    category: str = "",
    as_of: str | None = None,
    include_forgotten: bool = False,
    show_expired: bool = False,
    now: datetime | None = None,
    stability: str = "",
) -> list[AtomicFact]:
    """Facts matching the filters; with `as_of`, the facts valid at that moment.

    Expired facts (TTL passed) are hidden unless `show_expired` — the read
    half of the expiry contract `add --expires-at` writes.

    `stability` optionally keeps one supermemory tier ("stable" or "dynamic");
    the default "" keeps every tier, exactly like before the field existed.
    """
    if stability and stability not in STABILITY_TIERS:
        raise ValueError(f"unknown stability tier {stability!r} (expected 'stable' or 'dynamic')")
    moment = lesson_cache.parse_moment(as_of) if as_of else None
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    else:
        reference = reference.astimezone(timezone.utc)
    results: list[AtomicFact] = []
    facts = load_facts(root)
    resolver = EvidenceResolver(root, facts, as_of=as_of)
    from commontrace import memory_authority

    for fact in facts.values():
        if not include_forgotten and memory_authority.lineage_blocked(root, fact.id):
            continue
        if fact.forgotten and not include_forgotten:
            continue
        if stability and fact.stability != stability:
            continue
        if moment is None and status and fact.status != status:
            continue
        if category and fact.category != category:
            continue
        if scope and fact.scopes and scope not in fact.scopes:
            continue
        if moment is not None and not _valid_at(fact, moment):
            continue
        if not show_expired and _is_expired(fact, moment or reference):
            continue
        if fact.evidence_bound and not resolver.assess(fact.id).eligible:
            continue
        results.append(fact)
    return sorted(results, key=lambda f: (f.category, -f.confidence, f.id))


_TOKEN_RE = re.compile(r"[a-z0-9]+")

_FACT_TOKENS: dict[tuple[str, str, str], frozenset] = {}


def _fact_tokens(fact: AtomicFact) -> frozenset:
    """Token set memoized with the actual statement as well as id/revision.

    `search_facts` re-tokenized every fact on every query (mem0 precomputes
    `text_lemmatized` at write time; same idea, lazy). Imported ids/revisions
    can collide across stores or be stale. Actual text
    identity prevents one store's token set from changing another's rankings.
    The memo is capped to bound memory.
    """
    key = (fact.id, fact.revision, fact.statement)
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
    show_expired: bool = False,
    stability: str = "",
    *,
    scorer: str = "overlap-v1",
) -> list[tuple[AtomicFact, float]]:
    """Fresh governed sparse retrieval; overlap-v1 preserves legacy ranking.

    Optional bm25-v1 uses scope/time/metadata-filtered corpus statistics and
    English stemming and Unicode/CJK tokenization. Evidence is revalidated only for candidate results
    and their dependency ancestry; proof eligibility is never cached.
    """
    from commontrace.fact_index import search

    return search(root, query, scope=scope, category=category, as_of=as_of, limit=limit,
                  include_forgotten=include_forgotten, show_expired=show_expired,
                  stability=stability, scorer=scorer)


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
