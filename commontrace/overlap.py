"""Fleet Overlap: how much would fleet B gain from fleet A's lessons?

This is a deliberately bilateral, opt-in tool for two CONSENTING fleets who
have already agreed to compare notes -- distinct from, and unrelated to,
the CommonTrace Knowledge Base (hub/commons.py), which is a single
operator-curated corpus with no fleet-to-fleet data flow at all. It
measures *of the failures a fleet keeps hitting, what fraction has some
other, specific fleet already solved?* -- and it does so **without either
fleet sending the other any lesson or trace text.**

Mechanism: each side reduces every lesson/failure to a fixed-length MinHash
signature locally, and only signatures are exchanged. MinHash estimates the
Jaccard similarity of two token sets from their signatures alone
(Broder, 1997): for k independent hash permutations, the fraction of
positions where two signatures agree is an unbiased estimator of Jaccard
similarity, with standard error ~1/sqrt(k).

WHAT ACTUALLY TRAVELS -- read this before quoting it to a customer
------------------------------------------------------------------
Precision matters here, because the loose version of this claim ("no
content is shared") is false and a security reviewer will catch it.

NOT transmitted: lesson bodies, `applies_when` text, trace `context_text`
/ `solution_text`. Those are reduced to signatures, and the text cannot be
reconstructed from a signature.

DOES travel, by default:
  * `label` -- the lesson slug or a truncated trace id. A slug a human
    wrote, like `lesson_stripe_idempotency`, plainly describes the lesson.
    Pass redact_labels=True (CLI: `--redact-labels`) to replace these with
    salted hashes the owning fleet can map back locally.
  * `tags` and `domain` -- e.g. `stripe`, `webhooks`, `cuda-gpu`. These are
    an open, non-sensitive vocabulary by design (protocol/PROTOCOL.md §7)
    and they are what makes a report legible instead of a wall of opaque
    ids. They are still *information*: they reveal which technologies a
    fleet works with.

And even for the signed text, this is **not** a cryptographic guarantee.
MinHash is vulnerable to a confirmation attack: someone who can guess a
candidate string can hash it and check whether it is present. A production
commons spanning mutually distrustful organizations needs private set
intersection or a differential-privacy mechanism. Describe this module as
"lesson and trace text are not transmitted" -- never as "private".
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache

# Signature length. Standard error of the Jaccard estimate is ~1/sqrt(k),
# so 128 permutations gives ~8.8% -- fine for "is the overlap 5% or 40%?",
# which is the decision this exists to inform. Raise it if you ever need to
# separate 30% from 35%.
DEFAULT_NUM_PERM = 128

# A lesson counts as "already solves" a failure at or above this estimated
# Jaccard similarity. Deliberately conservative: this number will be quoted
# to customers, so it should under-claim rather than over-claim. Tune it
# against labelled pairs before moving it, not by eye.
DEFAULT_MATCH_THRESHOLD = 0.30

_MERSENNE_61 = (1 << 61) - 1

# Salt for label redaction. Fixed and public: the point is to stop a slug
# from *describing itself* to the other fleet, not to defeat a determined
# attacker who already has a candidate list. Anyone claiming stronger needs
# a keyed HMAC with a per-fleet secret, which then has to be managed.
_LABEL_SALT = b"commontrace-overlap-label-v1"
# \w with re.UNICODE, not [a-z0-9]: the ASCII-only class silently strips any
# non-Latin token before it ever reaches MinHash, so two fleets whose
# recurring failures are written in Japanese/Chinese/Cyrillic/Arabic (or any
# accented Latin text -- "connexion" survives, "connexión" does not) tokenize
# to an empty or near-empty signature and compare as ~0.0 Jaccard similarity
# to everything, including near-duplicates of themselves. Same fix already
# applied in commontrace/retrieval.py for the same reason.
_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Shared with commontrace/retrieval.py's ranker in spirit -- words that
# carry no discriminating signal would otherwise inflate every pairwise
# overlap and make unrelated fleets look similar -- but deliberately NOT the
# same literal list, and NOT imported from commontrace/_lexical.py the way
# retrieval.py/distill.py now share one copy between themselves. This exact
# word list feeds the MinHash signature this module computes, and that
# signature is what gets persisted (Trace.commons_signature, hub/crud.py)
# and compared against every future query. Changing so much as one word
# here silently reduces every already-stored signature's match quality
# against freshly-computed ones from that point on, with no error and no
# migration path -- so unlike the local, always-recomputed-on-the-fly
# copies in retrieval.py/distill.py, this one must not be casually "kept in
# sync" with them.
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
    """Stable opaque id for a label. The owning fleet can recompute this
    over its own slugs to map a report back to real lessons; the receiving
    fleet learns nothing from the id itself."""
    return hashlib.blake2b(label.encode("utf-8"), salt=_LABEL_SALT[:16], digest_size=6).hexdigest()


def _stable_hash(token: str) -> int:
    """Deterministic across processes and Python runs.

    Python's built-in hash() is randomized per process (PYTHONHASHSEED), so
    two fleets hashing the same word would get different values and every
    overlap would read as zero.
    """
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")


@lru_cache(maxsize=8)
def _permutations(num_perm: int) -> list[tuple[int, int]]:
    """(a, b) coefficients for the universal hash family h_i(x) = a_i*x + b_i
    mod prime. Seeded so every fleet derives the identical family -- signatures
    are only comparable if both sides used the same permutations.

    Pure function of `num_perm` (fixed seed), so cached: `minhash()` calls
    this once per lesson/failure signed, and `find_contradictions` signs
    every active lesson in one process -- redrawing the identical 128-pair
    table from scratch each time was pure waste that scaled with fleet size.
    maxsize=8 covers realistic distinct num_perm values in one run (the CLI
    default plus the rare custom --num-perm) without unbounded growth.
    """
    rng = random.Random(0xC0FFEE)
    return [(rng.randrange(1, _MERSENNE_61), rng.randrange(0, _MERSENNE_61)) for _ in range(num_perm)]


def minhash(text: str, num_perm: int = DEFAULT_NUM_PERM) -> list[int]:
    """MinHash signature of `text`'s token set. Empty/stopword-only text
    yields a freshly-drawn random signature, which compares as similarity
    ~0 against anything -- including another empty/stopword-only text.

    An earlier version returned the same fixed all-max-value signature for
    every empty input, so two DIFFERENT lessons/failures that both happened
    to have blank or stopword-only activation text compared as Jaccard=1.0
    -- exactly the "matches everything" failure the docstring warned
    against, just triggered by another empty signature instead of by real
    content. `reliability.py:find_contradictions` hashes every active
    lesson in one process and compares them pairwise, so this reliably
    produced false "high severity" contradiction candidates between
    lessons that share nothing but an unset `applies_when`. A random draw
    per call has the same near-zero collision probability against real
    content that two genuinely unrelated real texts already rely on, and
    additionally never coincides with another empty draw.
    """
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


# --- What gets signed, and exchanged ---------------------------------------


@dataclass
class SignedItem:
    """One lesson or one recurring failure, reduced to a signature.

    `label`, `domain` and `tags` are carried in the clear so a report can
    point at something actionable. That IS a disclosure -- a slug like
    `lesson_stripe_idempotency` describes its own content. See this
    module's docstring for exactly what travels and `redact_label()` for
    the opt-out.
    """

    label: str
    kind: str  # "lesson" | "failure"
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
    """Everything one fleet exchanges. No lesson or trace *text*; labels,
    tags and domains do travel unless redacted -- see the module docstring."""

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


# --- The report ------------------------------------------------------------


@dataclass
class Match:
    failure_label: str
    lesson_label: str
    similarity: float
    domain: str


@dataclass
class OverlapReport:
    """`covered_fraction` is the headline: of this fleet's recurring
    failures, what fraction another fleet has already solved. That is the
    number STRATEGY.md §5 says the commons thesis lives or dies on."""

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
    """For each of `consumer`'s recurring failures, find `provider`'s most
    similar lesson; count it covered if that similarity clears `threshold`.

    Deliberately asymmetric. "What would B gain from A?" is the question a
    prospective customer asks, and it is not the same as "how alike are these
    two fleets" -- a large mature fleet can cover a small one almost
    entirely while gaining little in return.
    """
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
