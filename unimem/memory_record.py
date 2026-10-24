"""Additive, JSON-portable envelope for versioned memory events.

The envelope is independent of the existing Trace, Lesson and AtomicFact wire
formats. Legacy payloads remain intact inside it.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from unimem.hierarchical import AtomicFact

MemoryKind = Literal["episode", "fact", "entity", "observation", "profile", "lesson", "directive"]

MAX_RECORD_BYTES = 1_048_576
MAX_RECORD_NODES = 100_000
MAX_RECORD_DEPTH = 64


def _finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    kind: MemoryKind
    version: int
    payload: dict[str, Any]
    status: str = "active"
    is_latest: bool = True
    relations: dict[str, list[str]] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=dict)
    valid_from: str | None = None
    valid_until: str | None = None
    created_at: str = ""
    retracted_at: str | None = None
    confidence: float = 0.8
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.id, str)
            or not self.id
            or len(self.id) > 256
            or self.kind
            not in (
                "episode",
                "fact",
                "entity",
                "observation",
                "profile",
                "lesson",
                "directive",
            )
        ):
            raise ValueError("record requires an id and a supported kind")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("record version must be positive")
        if not _finite_number(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("record confidence must be finite in [0, 1]")
        if self.status not in ("proposed", "quarantined", "shadow", "active", "deprecated", "retired"):
            raise ValueError("unsupported memory status")
        if type(self.is_latest) is not bool or not isinstance(self.payload, dict):
            raise ValueError("latest flag and payload have invalid types")
        if (not isinstance(self.provenance, dict) or not isinstance(self.evidence, list)
                or any(not isinstance(item, dict) for item in self.evidence)):
            raise ValueError("provenance and evidence have invalid types")
        revisions = self.provenance.get("source_versions")
        if revisions is not None and (
            not isinstance(revisions, dict)
            or any(not isinstance(key, str) or type(value) is not int or value < 1 for key, value in revisions.items())
        ):
            raise ValueError("source_versions must map source ids to positive integer revisions")
        times = {}
        for name in ("created_at", "valid_from", "valid_until", "retracted_at"):
            value = getattr(self, name)
            if value is None and name != "created_at":
                continue
            if not isinstance(value, str):
                raise ValueError("memory timestamps must be ISO 8601 strings or optional null")
            if not value:
                continue
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
                times[name] = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
            except (ValueError, AttributeError, TypeError) as exc:
                raise ValueError("memory timestamps must be ISO 8601") from exc
        if "valid_from" in times and "valid_until" in times and times["valid_until"] < times["valid_from"]:
            raise ValueError("validity end cannot precede validity start")
        if self.payload.get("forget_after"):
            try:
                datetime.fromisoformat(self.payload["forget_after"].replace("Z", "+00:00"))
            except (ValueError, AttributeError, TypeError) as exc:
                raise ValueError("forget_after must be ISO 8601") from exc
            if not isinstance(self.payload.get("forget_reason"), str) or not self.payload["forget_reason"].strip():
                raise ValueError("forget_after requires forget_reason")
        if self.kind == "profile":
            if self.payload.get("observed_at"):
                try:
                    datetime.fromisoformat(self.payload["observed_at"].replace("Z", "+00:00"))
                except (ValueError, AttributeError, TypeError) as exc:
                    raise ValueError("profile observed_at must be ISO 8601") from exc
            for name, default in (("weight", 1), ("half_life_days", 90)):
                value = self.payload.get(name, default)
                if not _finite_number(value) or value <= 0:
                    raise ValueError("profile weight and half-life must be finite and positive")
        # Run generic structural checks after field-specific validation so
        # callers retain precise errors for envelope fields such as confidence.
        _validate_record_json(self.__dict__)
        if not isinstance(self.relations, dict) or not isinstance(self.scope, dict):
            raise ValueError("scope and relationships must be objects")
        if set(self.relations) - {"updates", "extends", "derives", "supersedes", "caused_by"}:
            raise ValueError("unsupported memory relationship")
        if any(
            not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values)
            for values in self.relations.values()
        ):
            raise ValueError("relationships must be lists of nonempty id strings")
        if set(self.scope) - {"tenant", "user", "agent", "session", "repo", "labels"}:
            raise ValueError("unsupported record scope")
        if any(not isinstance(v, str) or not v or len(v) > 256 for k, v in self.scope.items() if k != "labels"):
            raise ValueError("scope dimensions must be bounded nonempty strings")
        labels = self.scope.get("labels", [])
        if not isinstance(labels, list) or any(not isinstance(v, str) or len(v) > 256 for v in labels):
            raise ValueError("scope labels must be bounded strings")
        if len(labels) > 128:
            raise ValueError("scope labels must be bounded")

    def validate(self) -> None:
        """Recheck mutable nested fields before serialization or durable writes."""
        self.__post_init__()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


def _validate_record_json(value: Any) -> None:
    """Reject nonportable, cyclic or oversized envelope metadata in linear time."""
    ancestors: set[int] = set()
    pending: list[tuple[Any, int, bool]] = [(value, 0, False)]
    nodes = 0
    encoded_bytes = 0

    def string_size(text: str) -> int:
        return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))

    while pending:
        item, depth, exiting = pending.pop()
        if exiting:
            ancestors.remove(id(item))
            continue
        nodes += 1
        if nodes > MAX_RECORD_NODES or depth > MAX_RECORD_DEPTH:
            raise ValueError("memory record metadata exceeds structural limits")
        if item is None:
            encoded_bytes += 4
        elif type(item) is bool:
            encoded_bytes += 4 if item else 5
        elif type(item) is int:
            if item.bit_length() > 4096:
                raise ValueError("memory record integer exceeds its encoding bound")
            encoded_bytes += len(str(item))
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("memory record numbers must be finite")
            encoded_bytes += len(json.dumps(item, allow_nan=False))
        elif type(item) is str:
            encoded_bytes += string_size(item)
        elif type(item) in (dict, list, tuple):
            identity = id(item)
            if identity in ancestors:
                raise ValueError("memory record metadata cannot contain cycles")
            if nodes + len(pending) + len(item) > MAX_RECORD_NODES:
                raise ValueError("memory record metadata exceeds structural limits")
            ancestors.add(identity)
            pending.append((item, depth, True))
            encoded_bytes += 2 + max(0, len(item) - 1)
            if type(item) is dict:
                for key, nested in reversed(item.items()):
                    if not isinstance(key, str):
                        raise ValueError("memory record object keys must be strings")
                    encoded_bytes += string_size(key) + 1
                    if encoded_bytes > MAX_RECORD_BYTES:
                        raise ValueError("memory record exceeds the one mebibyte wire limit")
                    pending.append((nested, depth + 1, False))
            else:
                for nested in reversed(item):
                    pending.append((nested, depth + 1, False))
        else:
            raise ValueError("memory record metadata must contain JSON values only")
        if encoded_bytes > MAX_RECORD_BYTES:
            raise ValueError("memory record exceeds the one mebibyte wire limit")


_ATOMIC_FACT_FORMAT = "unimem.atomic-fact.v1"
_ATOMIC_SCOPE_DIMENSIONS = frozenset({"tenant", "user", "agent", "session", "repo"})


def _atomic_fact_scope(scopes: list[str], tenant: str | None = None) -> dict[str, Any]:
    scope: dict[str, Any] = {}
    labels = []
    for label in scopes:
        dimension, separator, value = label.partition(":")
        if separator and dimension in _ATOMIC_SCOPE_DIMENSIONS:
            if not value or len(value) > 256 or dimension in scope and scope[dimension] != value:
                raise ValueError("conflicting or invalid AtomicFact scope dimension")
            scope[dimension] = value
        else:
            if len(label) > 256:
                raise ValueError("AtomicFact scope label exceeds envelope bounds")
            labels.append(label)
    if tenant is not None:
        if not isinstance(tenant, str) or not tenant or len(tenant) > 256:
            raise ValueError("tenant must be a bounded nonempty string")
        if "tenant" in scope and scope["tenant"] != tenant:
            raise ValueError("AtomicFact tenant does not match hosted tenant")
        scope["tenant"] = tenant
    if labels:
        scope["labels"] = labels
    return scope


def _atomic_fact_payload(payload: dict[str, Any]) -> AtomicFact:
    """Validate the exact bridge wire; legacy migration coercion would lose data."""
    import copy

    from unimem.fact_evidence import MAX_EVIDENCE, FactEvidence
    from unimem.hierarchical import AtomicFact

    fields = set(AtomicFact.__dataclass_fields__)
    if set(payload) != fields | {"text"}:
        raise ValueError("AtomicFact envelope must preserve exactly every fact field and text")
    for name in ("id", "statement", "category", "valid_from", "status", "revision", "created_at",
                 "updated_at", "stability", "evidence_revision", "memory_type"):
        if not isinstance(payload[name], str):
            raise ValueError(f"AtomicFact {name} must be a string")
    if not payload["id"] or len(payload["id"]) > 256 or not payload["statement"].strip():
        raise ValueError("AtomicFact requires an id and nonempty statement")
    if payload["text"] != payload["statement"]:
        raise ValueError("AtomicFact text must mirror statement")
    for name in ("valid_until", "expires_at", "superseded_by", "retracted_at"):
        if payload[name] is not None and not isinstance(payload[name], str):
            raise ValueError(f"AtomicFact {name} must be a string or null")
    for name in ("valid_from", "valid_until", "expires_at", "created_at", "updated_at", "retracted_at"):
        value = payload[name]
        if value:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"AtomicFact {name} must be ISO 8601 or blank") from exc
    for name in ("scopes", "source_traces"):
        if not isinstance(payload[name], list) or any(not isinstance(value, str) for value in payload[name]):
            raise ValueError(f"AtomicFact {name} must be a list of strings")
    for name in ("forgotten", "evidence_bound"):
        if type(payload[name]) is not bool:
            raise ValueError(f"AtomicFact {name} must be boolean")
    confidence = payload["confidence"]
    if type(confidence) not in (int, float) or not 0 <= confidence <= 1 or not math.isfinite(confidence):
        raise ValueError("AtomicFact confidence must be finite in [0, 1]")
    if type(payload["confirmations"]) is not int or payload["confirmations"] < 0:
        raise ValueError("AtomicFact confirmations must be a nonnegative integer")
    if type(payload["min_support"]) is not int or not 1 <= payload["min_support"] <= MAX_EVIDENCE:
        raise ValueError("AtomicFact min_support is outside evidence bounds")
    if payload["status"] not in {"active", "superseded", "invalidated", "deleted", "retired"}:
        raise ValueError("unsupported AtomicFact status")
    if payload["stability"] not in {"", "stable", "dynamic"}:
        raise ValueError("unsupported AtomicFact stability")
    if not isinstance(payload["origin"], dict):
        raise ValueError("AtomicFact origin must be a JSON object")

    def json_value(value: Any) -> bool:
        if value is None or type(value) in (str, bool, int):
            return True
        if type(value) is float:
            return math.isfinite(value)
        if type(value) is list:
            return all(json_value(item) for item in value)
        if type(value) is dict:
            return all(isinstance(key, str) and json_value(item) for key, item in value.items())
        return False

    try:
        portable = json_value(payload["origin"])
    except RecursionError as exc:
        raise ValueError("AtomicFact origin must be finite JSON") from exc
    if not portable:
        raise ValueError("AtomicFact origin must be finite JSON")
    if not isinstance(payload["evidence"], list) or len(payload["evidence"]) > MAX_EVIDENCE:
        raise ValueError("invalid AtomicFact evidence ledger")
    try:
        evidence = [FactEvidence.from_dict(row) for row in payload["evidence"]]
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("invalid AtomicFact evidence receipt") from exc
    values = copy.deepcopy({name: payload[name] for name in fields})
    values["evidence"] = evidence
    return AtomicFact(**values)


def atomic_fact_record(fact: AtomicFact, version: int, *, tenant: str | None = None) -> MemoryRecord:
    """Opt an exact local fact into the hosted envelope without legacy coercion."""
    from unimem.hierarchical import AtomicFact

    if type(fact) is not AtomicFact:
        raise ValueError("atomic_fact_record requires an AtomicFact")
    if type(version) is not int or version < 1:
        raise ValueError("record version must be a positive integer")
    payload = {**fact.to_dict(), "text": fact.statement}
    checked = _atomic_fact_payload(payload)
    effective_end = checked.valid_until
    if checked.expires_at:
        def instant(value: str) -> datetime:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed

        if not effective_end or instant(checked.expires_at) < instant(effective_end):
            effective_end = checked.expires_at
    return MemoryRecord(
        checked.id, "fact", version, payload,
        status=("active" if checked.status == "active" and not checked.forgotten else
                "retired" if checked.status == "retired" else "deprecated"),
        relations={"updates": [checked.id]} if version > 1 else {},
        provenance={"atomic_fact_format": _ATOMIC_FACT_FORMAT, "source_traces": checked.source_traces.copy()},
        scope=_atomic_fact_scope(checked.scopes, tenant), valid_from=checked.valid_from,
        valid_until=effective_end, created_at=checked.created_at, retracted_at=checked.retracted_at,
        confidence=checked.confidence, evidence=[receipt.to_dict() for receipt in checked.evidence],
    )


def atomic_fact_from_record(record: MemoryRecord) -> AtomicFact:
    """Decode only the marked bridge, rejecting inconsistent envelope mirrors."""
    if not isinstance(record, MemoryRecord) or record.kind != "fact" or \
            record.provenance.get("atomic_fact_format") != _ATOMIC_FACT_FORMAT:
        raise ValueError("record is not a supported AtomicFact envelope")
    fact = _atomic_fact_payload(record.payload)
    expected = atomic_fact_record(fact, record.version, tenant=record.scope.get("tenant"))
    for name in ("id", "status", "scope", "valid_from", "valid_until", "created_at", "retracted_at",
                 "confidence", "evidence"):
        if getattr(record, name) != getattr(expected, name):
            raise ValueError(f"AtomicFact envelope {name} does not mirror payload")
    if record.provenance.get("source_traces") != fact.source_traces:
        raise ValueError("AtomicFact provenance source_traces do not mirror payload")
    return fact


def facet_observation(
    entity: str,
    facet: str,
    sources: list[MemoryRecord],
    *,
    previous: MemoryRecord | None = None,
    text: str | None = None,
) -> MemoryRecord:
    """One stable, revision-grounded observation per entity/facet and exact scope."""
    import hashlib
    import json

    if not sources or not entity.strip() or not facet.strip() or len(sources) > 9:
        raise ValueError("observation requires a facet and at most nine grounding facts")
    if any(
        record.kind != "fact" or record.status != "active" or record.scope != sources[0].scope for record in sources
    ):
        raise ValueError("observation requires active facts from one scope")
    identity = (
        "observation-"
        + hashlib.sha256(json.dumps([entity, facet, sources[0].scope], sort_keys=True).encode()).hexdigest()
    )
    if previous is not None and previous.id != identity:
        raise ValueError("observation identity cannot change")
    return MemoryRecord(
        identity,
        "observation",
        previous.version + 1 if previous else 1,
        {"text": sources[-1].payload.get("text", "") if text is None else text, "entity": entity, "facet": facet},
        scope=sources[0].scope,
        relations={"derives": [record.id for record in sources]},
        created_at=datetime.now(timezone.utc).isoformat(),
        provenance={
            "source_fact_ids": [record.id for record in sources],
            "source_versions": {record.id: record.version for record in sources},
        },
    )


def preference_statistics(prior: dict[str, Any], *, at: str | None = None) -> dict[str, int | float]:
    """Reinforce effective weight, retaining decay since the previous assertion.

    Like Cognee's preference update, decay precedes new evidence rather than
    resetting the age of the raw accumulated weight. Legacy rows without an
    observation time retain their prior behavior; malformed decay metadata
    contributes no old weight. Both local and hosted capture use this helper.
    """
    weight = prior.get("weight", 0)
    if type(weight) not in (int, float) or not 0 <= weight <= 10000 or not math.isfinite(weight):
        weight = 0
    observed_at = prior.get("observed_at")
    if observed_at is not None and observed_at != "":
        from unimem.record_visibility import moment

        half_life = prior.get("half_life_days", 90)
        if type(half_life) not in (int, float) or not 0 < half_life <= 36500 or not math.isfinite(half_life):
            weight = 0
        else:
            try:
                reference = moment(at) if at is not None else datetime.now(timezone.utc)
                age = max(0.0, (reference - moment(observed_at)).total_seconds() / 86400)
                weight *= 2 ** (-age / half_life)
            except (ValueError, TypeError, AttributeError):
                weight = 0
    observations = prior.get("observations", 0)
    if type(observations) is not int or not 0 <= observations <= 1000000:
        observations = 0
    return {"weight": min(10, weight + 1), "half_life_days": 90,
            "observations": min(1000000, observations + 1)}


def preference_candidates(text: str, scope: dict) -> list[tuple[str, dict]]:
    """Literal first-person assertions shared by local and hosted capture."""
    import hashlib
    import json
    import re

    found = {}
    for prefix, bucket, pattern in (
        ("static-", "static", r"\bI (?:work as|am a|am an)\s+([^.!?\n]{3,120})"),
        ("preference-", "dynamic", r"\bI (?:prefer|like|always use|want)\s+([^.!?\n]{3,300})"),
    ):
        for match in re.finditer(pattern, text, flags=re.I):
            assertion = match.group(0).strip()
            identity = (
                prefix + hashlib.sha256((json.dumps(scope, sort_keys=True) + assertion.casefold()).encode()).hexdigest()
            )
            found[identity] = {"text": assertion, "bucket": bucket, "author": "user"}
    return list(found.items())[:32]


_PREFERENCE_PREFIX = re.compile(r"^\s*I\s+(?:prefer|like|want|always\s+use)\s+", re.I)
_PREFERENCE_STOP_WORDS = frozenset(
    "a an and are as at be by for from i in is it of on or the to use with".split()
)
_PREFERENCE_NEGATION = re.compile(r"\b(?:no|not|never|avoid|without|against)\b", re.I)


def rerank_profile_results(results: list[dict], preferences: list[dict]) -> list[dict]:
    """Apply a small, bounded boost from explicit dynamic user preferences.

    The caller supplies only preferences visible in the current scope and
    already decayed on read. Candidate retrieval and its budget stay fixed;
    this reorders only that bounded result list. Scored candidates receive at
    most a 20% score boost; when the backend exposes only order, the same
    support can move a result by at most 20% of the candidate window.
    """
    if not results or not preferences:
        return results

    from unimem.search_recipes import terms

    weighted_terms = []
    for preference in preferences:
        if (
            not isinstance(preference, dict)
            or preference.get("bucket") != "dynamic"
            or preference.get("author") != "user"
        ):
            continue
        text = preference.get("text")
        weight = preference.get("weight")
        if not isinstance(text, str) or type(weight) not in (int, float):
            continue
        numeric_weight = float(cast(int | float, weight))
        if not 0 < numeric_weight <= 10000 or not math.isfinite(numeric_weight):
            continue
        assertion = _PREFERENCE_PREFIX.sub("", text, count=1)
        if assertion == text or _PREFERENCE_NEGATION.search(assertion):
            continue
        tokens = set(terms(assertion)) - _PREFERENCE_STOP_WORDS
        if tokens:
            weighted_terms.append((tokens, numeric_weight / (numeric_weight + 1.0)))
    if not weighted_terms:
        return results

    ranked = []
    for position, row in enumerate(results):
        if not isinstance(row, dict):
            ranked.append((-(1.0 / (position + 1)), position, row))
            continue
        candidate_text = row.get("text")
        if not isinstance(candidate_text, str):
            payload = row.get("payload")
            candidate_text = payload.get("text", "") if isinstance(payload, dict) else ""
        candidate_terms = set(terms(candidate_text)) if isinstance(candidate_text, str) else set()

        support = max(
            (
                len(candidate_terms & pref_terms) / len(pref_terms) * strength
                for pref_terms, strength in weighted_terms
            ),
            default=0.0,
        )
        base_score = row.get("score")
        numeric_score = 0.0
        finite_score = type(base_score) in (int, float)
        if finite_score:
            try:
                numeric_score = float(cast(int | float, base_score))
            except (OverflowError, TypeError, ValueError):
                finite_score = False
            else:
                finite_score = math.isfinite(numeric_score)
        if not finite_score:
            # Hosted record search exposes an order but no numeric relevance.
            base_score = -position + 0.2 * len(results) * support
        else:
            base_score = numeric_score * (1.0 + 0.2 * support)
        ranked.append((-float(base_score), position, row))

    ranked.sort(key=lambda item: (item[0], item[1]))
    return [row for _score, _position, row in ranked]
