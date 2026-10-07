"""Revision-bound source evidence for derived atomic facts.

Hindsight (https://arxiv.org/abs/2512.12818) separates observations from their
supporting memories; Zep (https://arxiv.org/abs/2501.13956) preserves valid-time
boundaries. These contracts apply those ideas without model judgments: a cited
source is an attestation, not a calibrated probability or independent experiment.
The caller attests the support/refutation relationship; receipts verify source
attribution and currency, not logical entailment of a newly synthesized claim.
Current revocation and forgotten-source checks apply even to historical queries.
An in-place content correction cannot reconstruct its former body from a hash.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from commontrace.hierarchical import AtomicFact

EvidenceKind = Literal["fact", "lesson"]
EvidencePolarity = Literal["support", "refute"]
MAX_EVIDENCE = 256
MAX_DEPTH = 16
MAX_CHECKS = 4096
_IDENTITY = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_REVISION = re.compile(r"^[a-f0-9]{64}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvidenceError(ValueError):
    """Malformed, stale, unavailable, or improperly scoped source evidence."""


@dataclass(frozen=True)
class FactEvidence:
    kind: EvidenceKind
    source_id: str
    revision: str
    polarity: EvidencePolarity = "support"
    recorded_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.kind not in ("fact", "lesson") or self.polarity not in ("support", "refute"):
            raise EvidenceError("unknown evidence kind or polarity")
        if not isinstance(self.source_id, str) or not _IDENTITY.fullmatch(self.source_id):
            raise EvidenceError("evidence source_id must be a bounded canonical identifier")
        if not isinstance(self.revision, str) or not _REVISION.fullmatch(self.revision):
            raise EvidenceError("evidence revision must be a SHA256 content digest")
        try:
            recorded = datetime.fromisoformat(self.recorded_at.replace("Z", "+00:00"))
            if recorded.tzinfo is None:
                raise ValueError("missing timezone")
        except (ValueError, AttributeError, TypeError) as exc:
            raise EvidenceError("evidence recorded_at must be a timezone-aware instant") from exc

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.kind, self.source_id, self.revision, self.polarity

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: Mapping[str, object]) -> FactEvidence:
        if set(row) != {"kind", "source_id", "revision", "polarity", "recorded_at"}:
            raise EvidenceError("invalid evidence receipt fields")
        values = {key: value for key, value in row.items() if isinstance(value, str)}
        if len(values) != 5:
            raise EvidenceError("evidence receipt values must be strings")
        kind, polarity = values["kind"], values["polarity"]
        if kind not in ("fact", "lesson") or polarity not in ("support", "refute"):
            raise EvidenceError("unknown evidence kind or polarity")
        return cls(cast(EvidenceKind, kind), values["source_id"], values["revision"],
                   cast(EvidencePolarity, polarity), values["recorded_at"])


def claim_revision(fact: AtomicFact) -> str:
    """Hash actual claim content and provenance, independently of a stored hash.

    Operational counters and the fact's write timestamps do not alter claim
    content. Evidence receipt timestamps remain part of admitted provenance.
    Closing a validity window is checked at query valid-time instead of making
    the prior historical claim look like a body correction.
    """
    payload = {
        "statement": fact.statement, "category": fact.category, "scopes": sorted(fact.scopes),
        "valid_from": fact.valid_from, "expires_at": fact.expires_at, "stability": fact.stability,
        "evidence": [receipt.to_dict() for receipt in fact.evidence],
        "min_support": fact.min_support,
        "evidence_bound": fact.evidence_bound,
        "source_traces": sorted(fact.source_traces),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def scope_allows(target: Sequence[str], source: Sequence[str]) -> bool:
    """A private source may support only an equally or more narrowly scoped fact."""
    return not source or bool(target) and set(target) <= set(source)


@dataclass(frozen=True)
class EvidenceAssessment:
    """Current distinct cited sources; these counts are not independent trials."""
    status: Literal["unbound", "supported", "insufficient", "refuted", "stale", "invalid"]
    supports: int = 0
    refutes: int = 0
    reasons: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return self.status in ("unbound", "supported")

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "supports": self.supports,
                "refutes": self.refutes, "reasons": list(self.reasons)}


class EvidenceResolver:
    """One query snapshot with bounded recursive dependency validation.

    Fact rows are supplied by the caller's coherent snapshot. Lesson content and
    admission are re-read once per source; memoization never outlives a request.
    A bound claim requires *every* cited dependency to be current and eligible,
    avoiding silent proof loss when a source is corrected or revoked.
    ``as_of`` reconstructs valid-time windows using currently admitted proof;
    it does not pretend to reconstruct what the system knew at recorded time.
    """

    def __init__(self, root: str, facts: Mapping[str, AtomicFact], *, as_of: str | None = None,
                 max_depth: int = MAX_DEPTH, max_checks: int = MAX_CHECKS) -> None:
        if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 1 <= max_depth <= MAX_DEPTH:
            raise EvidenceError(f"max_depth must be an integer between 1 and {MAX_DEPTH}")
        if isinstance(max_checks, bool) or not isinstance(max_checks, int) or not 1 <= max_checks <= MAX_CHECKS:
            raise EvidenceError(f"max_checks must be an integer between 1 and {MAX_CHECKS}")
        self.root, self.facts, self.as_of = root, facts, as_of
        self.max_depth, self.max_checks = max_depth, max_checks
        self._memo: dict[str, EvidenceAssessment] = {}
        self._lessons: dict[str, tuple[str, list[str]] | None] = {}

    def _fact_live(self, fact: AtomicFact) -> bool:
        from commontrace import hierarchical, lesson_cache

        moment = lesson_cache.parse_moment(self.as_of) if self.as_of else datetime.now(timezone.utc)
        return not fact.forgotten and fact.status != "deleted" and (bool(self.as_of) or fact.status == "active") \
            and hierarchical._valid_at(fact, moment) and not hierarchical._is_expired(fact, moment)

    def _lesson(self, identity: str) -> tuple[str, list[str]] | None:
        if identity in self._lessons:
            return self._lessons[identity]
        from commontrace import frontmatter, lesson_admission, lesson_cache, lesson_io

        value = None
        path = lesson_io.lesson_path(self.root, identity)
        if path is not None:
            try:
                fm, body = frontmatter.read(path)
                name = fm.get("name")
                if isinstance(name, str) and lesson_io.canonical_slug(name) == identity \
                        and fm.get("status", "active") == "active" and lesson_admission.eligible(
                    self.root, path, fm, body, as_of=self.as_of,
                ) and lesson_cache.fresh_eligible(path, fm, name, as_of=self.as_of, body=body, root=self.root):
                    scopes = fm.get("scopes") or []
                    if isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes):
                        value = lesson_admission.digest_of(fm, body), scopes
            except (OSError, ValueError, TypeError):
                value = None
        self._lessons[identity] = value
        return value

    def source(self, receipt: FactEvidence) -> tuple[str, list[str]] | None:
        if receipt.kind == "lesson":
            return self._lesson(receipt.source_id)
        source = self.facts.get(receipt.source_id)
        if source is None or not self._fact_live(source):
            return None
        return claim_revision(source), source.scopes

    def source_quote(self, receipt: FactEvidence) -> str | None:
        """Return an actual source body only while its admitted digest matches.

        A second lesson read is verified as one snapshot; a correction between
        dependency assessment and quote assembly cannot acquire an old citation.
        """
        if receipt.kind == "fact":
            fact = self.facts.get(receipt.source_id)
            if fact is not None and self._fact_live(fact) and claim_revision(fact) == receipt.revision:
                statement = fact.statement
                return statement if isinstance(statement, str) else None
            return None
        from commontrace import frontmatter, lesson_admission, lesson_cache, lesson_io

        path = lesson_io.lesson_path(self.root, receipt.source_id)
        if path is not None:
            try:
                fm, body = frontmatter.read(path)
                name = fm.get("name")
                if isinstance(name, str) and lesson_io.canonical_slug(name) == receipt.source_id \
                        and lesson_admission.digest_of(fm, body) == receipt.revision \
                        and lesson_cache.fresh_eligible(
                    path, fm, name, as_of=self.as_of, body=body, root=self.root,
                ):
                    return body if isinstance(body, str) else None
            except (OSError, ValueError, TypeError):
                pass
        return None

    def assess(self, fact_id: str) -> EvidenceAssessment:
        return self._assess(fact_id, (), [self.max_checks])

    def _assess(self, fact_id: str, ancestors: tuple[str, ...], budget: list[int]) -> EvidenceAssessment:
        if fact_id in ancestors:
            return EvidenceAssessment("invalid", reasons=("dependency cycle",))
        if len(ancestors) >= self.max_depth or budget[0] <= 0:
            return EvidenceAssessment("invalid", reasons=("dependency validation budget exceeded",))
        if fact_id in self._memo:
            return self._memo[fact_id]
        budget[0] -= 1
        fact = self.facts.get(fact_id)
        if fact is None:
            return EvidenceAssessment("invalid", reasons=("missing fact",))
        if not fact.evidence_bound:
            return EvidenceAssessment("unbound")
        if not fact.evidence_revision or claim_revision(fact) != fact.evidence_revision:
            return EvidenceAssessment("stale", reasons=("derived claim content changed since evidence admission",))
        support: set[tuple[str, str]] = set()
        refute: set[tuple[str, str]] = set()
        reasons: set[str] = set()
        groups: dict[tuple[str, str], list[FactEvidence]] = {}
        for receipt in fact.evidence:
            groups.setdefault((receipt.kind, receipt.source_id), []).append(receipt)
        for receipts in groups.values():
            receipt = receipts[0]
            budget[0] -= 1
            if budget[0] < 0:
                reasons.add("dependency validation budget exceeded")
                break
            source = self.source(receipt)
            current = [r for r in receipts if source is not None and source[0] == r.revision]
            if not current:
                reasons.add("missing, ineligible, or changed source")
                continue
            assert source is not None
            if not scope_allows(fact.scopes, source[1]):
                reasons.add("source scope does not authorize derived fact")
                continue
            if receipt.kind == "fact":
                dependency = self._assess(receipt.source_id, (*ancestors, fact_id), budget)
                if not dependency.eligible:
                    reasons.add("source fact evidence is not eligible")
                    continue
            for current_receipt in current:
                (support if current_receipt.polarity == "support" else refute).add((receipt.kind, receipt.source_id))
        if reasons:
            assessment = EvidenceAssessment("stale", len(support), len(refute), tuple(sorted(reasons)))
        elif refute:
            assessment = EvidenceAssessment("refuted", len(support), len(refute), ("live refuting evidence",))
        elif len(support) < fact.min_support:
            assessment = EvidenceAssessment("insufficient", len(support), reasons=("insufficient distinct sources",))
        else:
            assessment = EvidenceAssessment("supported", len(support))
        self._memo[fact_id] = assessment
        return assessment

    def validate_receipts(self, receipts: Sequence[FactEvidence], scopes: Sequence[str]) -> None:
        if len(receipts) > MAX_EVIDENCE:
            raise EvidenceError(f"a fact may retain at most {MAX_EVIDENCE} evidence receipts")
        for receipt in receipts:
            if not isinstance(receipt, FactEvidence):
                raise EvidenceError("evidence must contain FactEvidence receipts")
            source = self.source(receipt)
            if source is None or source[0] != receipt.revision or not scope_allows(scopes, source[1]):
                raise EvidenceError("source evidence is unavailable, changed, or outside the target scope")
            if receipt.kind == "fact" and not self.assess(receipt.source_id).eligible:
                raise EvidenceError("source fact does not have eligible evidence")


def bind_evidence(root: str, kind: EvidenceKind, source_id: str, *,
                  polarity: EvidencePolarity = "support", revision: str | None = None) -> FactEvidence:
    """Capture a current source digest; an explicit stale digest is rejected."""
    from commontrace import hierarchical, lesson_io

    identity = lesson_io.canonical_slug(source_id) if kind == "lesson" else source_id
    provisional = FactEvidence(kind, identity, revision or "0" * 64, polarity)
    resolver = EvidenceResolver(root, hierarchical.load_facts(root))
    source = resolver.source(provisional)
    if source is None or revision is not None and source[0] != revision:
        raise EvidenceError("source evidence is unavailable or changed")
    if kind == "fact" and not resolver.assess(identity).eligible:
        raise EvidenceError("source fact does not have eligible evidence")
    return FactEvidence(kind, identity, source[0], polarity)


def parse_evidence(root: str, rows: Sequence[Mapping[str, object]]) -> list[FactEvidence]:
    """Bind untrusted tool receipt arguments; recorded time is set by this store."""
    if len(rows) > MAX_EVIDENCE:
        raise EvidenceError(f"at most {MAX_EVIDENCE} evidence receipts may be submitted")
    output = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) - {"kind", "source_id", "revision", "polarity"}:
            raise EvidenceError("invalid submitted evidence fields")
        kind, polarity = row.get("kind"), row.get("polarity", "support")
        identity, revision = row.get("source_id"), row.get("revision")
        if kind not in ("fact", "lesson") or polarity not in ("support", "refute") \
                or not isinstance(identity, str) or revision is not None and not isinstance(revision, str):
            raise EvidenceError("invalid submitted evidence values")
        output.append(bind_evidence(root, kind, identity, polarity=polarity, revision=revision))
    return output
