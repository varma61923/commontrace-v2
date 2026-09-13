"""What the agent could have seen, what it was given, and what it used.

WHY THIS EXISTS
---------------
The holdout log (commontrace/holdout_io.py) records ELIGIBILITY and ARM: for
each (lesson, occasion), the lesson matched and was then injected or
withheld. That is exactly what the causal comparison needs and it is not
what an audit needs, because it starts one step too late. It cannot answer:

  * **What else was there?** A lesson that was never a candidate does not
    appear at all, so "the memory did not help" and "the memory was never
    offered" are indistinguishable afterwards -- and they have opposite
    remedies.
  * **What did the agent actually read?** Retrieval returns a ranked list;
    a budget (commontrace/dosage.py) then decides how much of it is
    injected. An occasion where the right lesson ranked fourth and the
    budget admitted three is a retrieval success and a product failure, and
    nothing distinguished it from a ranking failure.
  * **Did it matter?** Injected is not used. Without that distinction, every
    reuse number this product reports is really an INJECTION number wearing
    a better name.

A receipt records all three for one occasion: the candidate set that was
visible, the subset that was actually admitted (with rank, relevance and the
budget that shaped it), and -- written later, when the outcome is known --
which of those the agent says it used.

BYTE-EXACT, AND WHY THAT WORD
-----------------------------
Each receipt stores every visible lesson's REVISION, not just its slug, and
a digest over that whole set. So a dispute six months later about "what did
the agent have in front of it" is answerable to the exact text, not to a
name whose contents have moved since. That is the property that makes a
receipt evidence rather than a log line, and it is why this is worth a file
of its own rather than three more columns on the holdout log.

APPEND-ONLY, AND NEVER RECONSTRUCTED
------------------------------------
Written at decision time, like the holdout log and for the same reason: a
candidate set reconstructed afterwards is a candidate set computed against
today's store, today's ranking, and today's budget. It would look
authoritative and describe a retrieval that never happened.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from dataclasses import dataclass, field

from commontrace import paths

RECEIPTS_FILENAME = "retrieval_receipts.jsonl"
USES_FILENAME = "retrieval_uses.jsonl"

_FIELD_SEP = "\x1f"
_DIGEST_DOMAIN = "commontrace-retrieval-receipt-v1"


@dataclass(frozen=True)
class Visible:
    """One lesson that was a candidate, pinned to the text it then had."""

    slug: str
    revision: str


@dataclass(frozen=True)
class Admitted:
    """One lesson the agent was actually given."""

    slug: str
    revision: str
    rank: int
    relevance: float = 0.0
    core: bool = False


@dataclass(frozen=True)
class Receipt:
    occasion_id: str
    at: str
    visible: tuple[Visible, ...] = field(default_factory=tuple)
    admitted: tuple[Admitted, ...] = field(default_factory=tuple)
    #: Why the admitted set is smaller than the visible one, per lesson.
    withheld: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    query: str = ""
    scorer: str = ""
    floor: float = 0.0
    chars_used: int = 0
    max_chars: int = 0
    max_lessons: int = 0
    #: The release the fleet was running, when one has been cut
    #: (commontrace/release.py). Ties a retrieval to a named deployment
    #: rather than to "whatever the files said at the time".
    release_id: str = ""

    @property
    def digest(self) -> str:
        """Identifies the exact candidate set, by content.

        Over (slug, revision) pairs sorted canonically, so two retrievals
        that saw the same store agree regardless of ranking order -- the
        digest answers "was the same knowledge available", which is a
        different question from "was it ranked the same way".
        """
        rows = _FIELD_SEP.join(
            f"{v.slug}={v.revision}" for v in sorted(self.visible, key=lambda v: v.slug)
        )
        return hashlib.sha256(
            (_DIGEST_DOMAIN + _FIELD_SEP + rows).encode("utf-8")
        ).hexdigest()

    @property
    def n_visible(self) -> int:
        return len(self.visible)

    @property
    def n_admitted(self) -> int:
        return len(self.admitted)

    def to_dict(self) -> dict:
        return {
            "occasion_id": self.occasion_id,
            "at": self.at,
            "query": self.query,
            "scorer": self.scorer,
            "floor": self.floor,
            "chars_used": self.chars_used,
            "max_chars": self.max_chars,
            "max_lessons": self.max_lessons,
            "release_id": self.release_id,
            "visible_digest": self.digest,
            "visible": [{"slug": v.slug, "revision": v.revision} for v in self.visible],
            "admitted": [
                {"slug": a.slug, "revision": a.revision, "rank": a.rank,
                 "relevance": a.relevance, "core": a.core}
                for a in self.admitted
            ],
            "withheld": [{"slug": s, "reason": r} for s, r in self.withheld],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Receipt:
        return cls(
            occasion_id=str(raw.get("occasion_id", "")),
            at=str(raw.get("at", "")),
            visible=tuple(
                Visible(str(v.get("slug", "")), str(v.get("revision", "")))
                for v in (raw.get("visible") or []) if isinstance(v, dict)
            ),
            admitted=tuple(
                Admitted(
                    slug=str(a.get("slug", "")), revision=str(a.get("revision", "")),
                    rank=int(a.get("rank", 0)), relevance=float(a.get("relevance", 0.0)),
                    core=bool(a.get("core", False)),
                )
                for a in (raw.get("admitted") or []) if isinstance(a, dict)
            ),
            withheld=tuple(
                (str(w.get("slug", "")), str(w.get("reason", "")))
                for w in (raw.get("withheld") or []) if isinstance(w, dict)
            ),
            query=str(raw.get("query", "")),
            scorer=str(raw.get("scorer", "")),
            floor=float(raw.get("floor", 0.0)),
            chars_used=int(raw.get("chars_used", 0)),
            max_chars=int(raw.get("max_chars", 0)),
            max_lessons=int(raw.get("max_lessons", 0)),
            release_id=str(raw.get("release_id", "")),
        )


def receipts_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), RECEIPTS_FILENAME)


def uses_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), USES_FILENAME)


def _append(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def record(root: str, receipt: Receipt) -> Receipt:
    """Write one receipt. Append-only; nothing rewrites an earlier line."""
    _append(receipts_path(root), receipt.to_dict())
    return receipt


def record_use(
    root: str,
    occasion_id: str,
    used: list[str],
    *,
    succeeded: bool | None = None,
    now: datetime.datetime | None = None,
) -> dict:
    """Record which admitted lessons the agent says it actually used.

    A SEPARATE line rather than an edit to the receipt, because the receipt
    is a record of a decision already made and this is a later, independent
    claim about it -- possibly by a different actor, certainly at a different
    time. Rewriting the earlier line would lose the fact that they were two
    events, which is exactly what an auditor is trying to establish.

    `used=[]` is a real answer and is stored as one: "the agent was given
    four lessons and used none" is the most informative outcome this product
    can collect, and the shape that most easily gets discarded as an empty
    value.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    entry = {
        "occasion_id": occasion_id,
        "at": moment.isoformat(),
        "used": sorted(set(used)),
        "succeeded": succeeded,
    }
    _append(uses_path(root), entry)
    return entry


def _read_jsonl(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record_ = json.loads(line)
            except ValueError:
                continue
            if isinstance(record_, dict):
                out.append(record_)
    return out


def read_all(root: str) -> list[Receipt]:
    return [Receipt.from_dict(r) for r in _read_jsonl(receipts_path(root))]


def read_uses(root: str) -> list[dict]:
    return _read_jsonl(uses_path(root))


@dataclass(frozen=True)
class Coverage:
    """What the receipts say about whether retrieval is doing its job."""

    n_occasions: int = 0
    n_with_any_admitted: int = 0
    n_with_any_used: int = 0
    #: Occasions where something was visible but the budget admitted none of
    #: it. A retrieval success and a product failure, and the pair this whole
    #: module exists to be able to tell apart.
    n_crowded_out: int = 0
    #: Lessons admitted but never reported used, by slug.
    never_used: tuple[str, ...] = field(default_factory=tuple)

    @property
    def admitted_rate(self) -> float:
        return self.n_with_any_admitted / self.n_occasions if self.n_occasions else 0.0

    @property
    def used_rate(self) -> float:
        """Of the occasions that received anything, how many used it.

        Deliberately NOT over all occasions: an occasion nothing was
        injected into cannot have used anything, and including it would
        blend a retrieval problem into a usefulness number.
        """
        return (
            self.n_with_any_used / self.n_with_any_admitted
            if self.n_with_any_admitted else 0.0
        )


def coverage(root: str) -> Coverage:
    """Join receipts against use reports.

    The one number this makes possible and nothing else did: the difference
    between injected and USED. Every "lessons reused" figure before this was
    counting injections.
    """
    receipts = read_all(root)
    uses = read_uses(root)
    used_by_occasion: dict[str, set[str]] = {}
    for entry in uses:
        occasion = str(entry.get("occasion_id", ""))
        used_by_occasion.setdefault(occasion, set()).update(entry.get("used") or [])

    admitted_slugs: set[str] = set()
    used_slugs: set[str] = set()
    with_admitted = with_used = crowded = 0
    for receipt in receipts:
        if receipt.admitted:
            with_admitted += 1
            admitted_slugs.update(a.slug for a in receipt.admitted)
        elif receipt.visible:
            crowded += 1
        used_here = used_by_occasion.get(receipt.occasion_id, set())
        if used_here:
            with_used += 1
            used_slugs.update(used_here)

    return Coverage(
        n_occasions=len(receipts),
        n_with_any_admitted=with_admitted,
        n_with_any_used=with_used,
        n_crowded_out=crowded,
        never_used=tuple(sorted(admitted_slugs - used_slugs)),
    )
