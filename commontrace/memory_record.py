"""Additive, JSON-portable envelope for versioned memory events.

The envelope is independent of the existing Trace, Lesson and AtomicFact wire
formats. Legacy payloads remain intact inside it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
        if not self.id or self.kind not in (
            "episode", "fact", "entity", "observation", "profile", "lesson", "directive",
        ):
            raise ValueError("record requires an id and a supported kind")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("record version must be positive")
        if not 0 <= self.confidence <= 1:
            raise ValueError("record confidence must be finite in [0, 1]")
        if set(self.relations) - {"updates", "extends", "derives", "supersedes", "caused_by"}:
            raise ValueError("unsupported memory relationship")
        if any(not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values)
               for values in self.relations.values()):
            raise ValueError("relationships must be lists of nonempty id strings")
        if set(self.scope) - {"tenant", "user", "agent", "session", "repo", "labels"}:
            raise ValueError("unsupported record scope")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
