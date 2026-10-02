"""Fleet Overlap: how much would fleet B gain from fleet A's lessons?"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache

DEFAULT_NUM_PERM = 128

DEFAULT_MATCH_THRESHOLD = 0.30

_MERSENNE_61 = (1 << 61) - 1

_LABEL_SALT = b"commontrace-overlap-label-v1"
_WORD_RE = re.compile(r"\w+", re.UNICODE)

_STOPWORDS = frozenset(
    """
    a an the of to in on for with and or but is are was were be been being
    this that these those it its as at by from into over under again further
    then once here there when where why how all any both each few more most
    other some such no nor not only own same so than too very can will just
    should now i you he she we they them his her our your their
    """.split()
)


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOPWORDS and len(w) > 1}


def redact_label(label: str) -> str:
    return hashlib.blake2b(label.encode("utf-8"), salt=_LABEL_SALT[:16], digest_size=6).hexdigest()


def _stable_hash(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")


@lru_cache(maxsize=8)
def _permutations(num_perm: int) -> list[tuple[int, int]]:
    rng = random.Random(0xC0FFEE)
    return [(rng.randrange(1, _MERSENNE_61), rng.randrange(0, _MERSENNE_61)) for _ in range(num_perm)]


def minhash(text: str, num_perm: int = DEFAULT_NUM_PERM) -> list[int]:
    toks = _tokens(text)
    perms = _permutations(num_perm)
    if not toks:
        return [random.SystemRandom().randrange(0, _MERSENNE_61) for _ in range(num_perm)]

    hashes = [_stable_hash(t) for t in toks]
    return [min((a * h + b) % _MERSENNE_61 for h in hashes) for a, b in perms]


def estimate_jaccard(sig_a: list[int], sig_b: list[int]) -> float:
    """Fraction of agreeing positions == unbiased Jaccard estimate."""
    if not sig_a or not sig_b or len(sig_a) != len(sig_b):
        raise ValueError("signatures must be non-empty and the same length")
    return sum(1 for x, y in zip(sig_a, sig_b) if x == y) / len(sig_a)


@dataclass
class SignedItem:
    """One lesson or one recurring failure, reduced to a signature."""

    label: str
    kind: str
    domain: str
    tags: list[str]
    signature: list[int]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> SignedItem:
        return cls(
            label=d["label"], kind=d["kind"], domain=d.get("domain", ""),
            tags=list(d.get("tags") or []), signature=list(d["signature"]),
        )


@dataclass
class FleetSignature:
    fleet_label: str
    num_perm: int
    items: list[SignedItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "fleet_label": self.fleet_label,
            "num_perm": self.num_perm,
            "items": [i.to_dict() for i in self.items],
        }

    @classmethod
    def from_dict(cls, d: dict) -> FleetSignature:
        return cls(
            fleet_label=d["fleet_label"],
            num_perm=int(d["num_perm"]),
            items=[SignedItem.from_dict(i) for i in d.get("items", [])],
        )

    def lessons(self) -> list[SignedItem]:
        return [i for i in self.items if i.kind == "lesson"]

    def failures(self) -> list[SignedItem]:
        return [i for i in self.items if i.kind == "failure"]


@dataclass
class Match:
    failure_label: str
    lesson_label: str
    similarity: float
    domain: str


@dataclass
class OverlapReport:
    consumer_fleet: str
    provider_fleet: str
    n_failures: int
    n_provider_lessons: int
    n_covered: int
    covered_fraction: float
    threshold: float
    matches: list[Match]
    by_domain: dict[str, int]
    note: str = ""


def build_report(
    consumer: FleetSignature,
    provider: FleetSignature,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> OverlapReport:
    if consumer.num_perm != provider.num_perm:
        raise ValueError(
            f"signature length mismatch ({consumer.num_perm} vs {provider.num_perm}); "
            "both fleets must sign with the same num_perm for signatures to be comparable"
        )

    failures = consumer.failures()
    lessons = provider.lessons()

    matches: list[Match] = []
    by_domain: dict[str, int] = {}
    for f in failures:
        best, best_sim = None, 0.0
        for lesson in lessons:
            sim = estimate_jaccard(f.signature, lesson.signature)
            if sim > best_sim:
                best, best_sim = lesson, sim
        if best is not None and best_sim >= threshold:
            matches.append(
                Match(failure_label=f.label, lesson_label=best.label,
                      similarity=round(best_sim, 4), domain=f.domain or best.domain)
            )
            key = f.domain or best.domain or "(none)"
            by_domain[key] = by_domain.get(key, 0) + 1

    matches.sort(key=lambda m: m.similarity, reverse=True)
    n_cov = len(matches)

    note = ""
    if not failures:
        note = (
            "No recurring failures found. A failure is a trace with "
            "outcome.repeated_error = true -- capture with `--repeated-error` "
            "(or import a column of that name) for this report to have input."
        )
    elif not lessons:
        note = "The provider fleet exported no lessons, so nothing could match."
    elif len(failures) < 20:
        note = (
            f"Only {len(failures)} recurring failures in the sample. Treat the "
            "percentage as directional; MinHash adds ~"
            f"{100 / (consumer.num_perm ** 0.5):.0f}% standard error per comparison "
            "on top of small-sample noise."
        )

    return OverlapReport(
        consumer_fleet=consumer.fleet_label,
        provider_fleet=provider.fleet_label,
        n_failures=len(failures),
        n_provider_lessons=len(lessons),
        n_covered=n_cov,
        covered_fraction=(n_cov / len(failures)) if failures else 0.0,
        threshold=threshold,
        matches=matches,
        by_domain=dict(sorted(by_domain.items(), key=lambda kv: -kv[1])),
        note=note,
    )


def render_report(r: OverlapReport) -> str:
    pct = r.covered_fraction * 100
    lines = [
        "# Fleet Overlap Report",
        "",
        f"**{r.consumer_fleet}** evaluated against **{r.provider_fleet}**",
        "",
        f"- Recurring failures analyzed: **{r.n_failures}**",
        f"- Lessons available from the other fleet: **{r.n_provider_lessons}**",
        f"- Already solved elsewhere: **{r.n_covered}** (**{pct:.1f}%**)",
        f"- Match threshold: {r.threshold} estimated Jaccard similarity",
        "",
    ]
    if r.note:
        lines += [f"> {r.note}", ""]

    if r.by_domain:
        lines += ["## Where the overlap is", "", "| Domain | Failures already solved |", "|---|---|"]
        lines += [f"| {d} | {n} |" for d, n in r.by_domain.items()]
        lines.append("")

    if r.matches:
        lines += [
            "## Top matches",
            "",
            "Labels only — no lesson or failure text was exchanged to produce this.",
            "",
            "| Your recurring failure | Solved by (other fleet) | Similarity |",
            "|---|---|---|",
        ]
        lines += [
            f"| `{m.failure_label}` | `{m.lesson_label}` | {m.similarity:.2f} |"
            for m in r.matches[:15]
        ]
        lines.append("")

    lines += [
        "---",
        "",
        "Signatures, not content, were exchanged (MinHash). Text cannot be "
        "reconstructed from them, but this is not a cryptographic privacy "
        "guarantee — see `commontrace/overlap.py` for what it does and does "
        "not protect against.",
    ]
    return "\n".join(lines)
