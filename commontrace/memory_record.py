"""Additive, JSON-portable envelope for versioned memory events.

The envelope is independent of the existing Trace, Lesson and AtomicFact wire
formats. Legacy payloads remain intact inside it.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

MemoryKind = Literal["episode", "fact", "entity", "observation", "profile", "lesson", "directive"]


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
        if (
            type(self.confidence) not in (float, int)
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("record confidence must be finite in [0, 1]")
        if self.status not in ("proposed", "quarantined", "shadow", "active", "deprecated", "retired"):
            raise ValueError("unsupported memory status")
        if type(self.is_latest) is not bool or not isinstance(self.payload, dict):
            raise ValueError("latest flag and payload have invalid types")
        if not isinstance(self.provenance, dict) or not isinstance(self.evidence, list):
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
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ValueError("profile weight and half-life must be finite and positive")
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
