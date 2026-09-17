"""Which lessons say the same thing -- near-duplicate detection, no embeddings.

WHY THIS EXISTS
---------------
Three separate places in this product needed the same question answered and
none of them could ask it:

  * **Injection** (`commontrace/dosage.py`). The budget is scarce -- ten
    lessons and 8,000 characters -- and admission was purely by rank. Two
    lessons that say the same thing in different words both rank highly for
    the same task, by construction, so they were both admitted and the
    second one bought the agent nothing while spending a slot and its
    characters. A store with a few redundant pairs silently injects less
    distinct guidance than its budget suggests.

  * **Corpus maintenance** (`commontrace consolidate`). DOCUMENTATION.md
    §6.4 describes "classic memory consolidation (archive/fuse/reformulate)"
    and delegates it to a companion skill that lives outside this
    repository, using pairwise cosine over `memory/attention/index.npz`.
    That makes corpus hygiene unavailable to every store that never
    installed the optional `attention` extra -- which is the default
    install.

  * **Authoring** (`commontrace lesson new`, MCP `draft_lesson`). Nothing
    checked whether a lesson being written already existed. An agent
    curating unattended re-derives the same rule from a different trace
    cluster and the corpus grows two copies, which then compete for the
    same retrieval slot forever.

WHY LEXICAL, AND WHAT THAT COSTS
--------------------------------
This is token-set Jaccard, not cosine over sentence embeddings. Two
reasons, in this order:

  1. The core install is PyYAML and nothing else (pyproject.toml). A
     redundancy check that needs `sentence-transformers` is a redundancy
     check most stores do not have, and "your corpus quality tooling
     requires a 2 GB torch download" is the kind of default that makes the
     feature theoretical.

  2. Everything else in the local tier already ranks this way
     (`commontrace/retrieval.py` is IDF-weighted lexical). A dedupe pass
     that used a *different* notion of similarity than the retriever would
     drop a lesson the retriever considers distinct, which is the worst
     available outcome: the agent loses guidance for a reason the ranking
     it can inspect does not explain.

The cost is real and is stated rather than hidden: two lessons that express
the same rule with no shared vocabulary ("never retry without an
idempotency key" / "duplicate charges come from unkeyed replays") score
near zero here and are not detected. This finds *restatements*, not
*paraphrases*. The optional semantic layer remains the better instrument
where it is installed, and `find_near_duplicates` accepts a caller-supplied
similarity function precisely so it can be driven by one.

WHAT IS COMPARED
----------------
`comparable_text` uses description + applies_when + do_not_apply_when +
body -- what the lesson SAYS and WHEN IT FIRES.

Deliberately NOT `tags` and `domain`: they are grouping metadata, shared by
construction among lessons in the same area, and including them adds a
constant similarity floor to exactly the pairs most likely to be compared.
Two genuinely distinct `domain: idempotency` lessons should not read as 30%
redundant because both carry the words "idempotency" and "payments".

Deliberately NOT `importance_rationale`, `source_traces` or any counter:
provenance and lifecycle are not what an agent reads.

HOW THE DEFAULT THRESHOLD WAS CHOSEN
------------------------------------
Measured, not guessed -- `commontrace/reference/measure_redundancy.py`
sweeps it over the two corpora that ship in this repository: the eight
field fixtures in `commontrace/fixtures/fields/` (48 lessons across eight
unrelated fields) and `commons/seed/substrate-v1.jsonl` (46 independently
authored entries, no shared authorship with the fixtures).

Neither corpus contains an intentional duplicate, so the calibration
statistic is not "how many pairs get flagged" but **the highest similarity
two genuinely distinct lessons reach** -- the number any honest threshold
has to clear:

    corpus                       pairs   max    p99    mean
    field fixtures (8 fields)    1,128   0.167  0.116  0.046
    commons seed                 1,035   0.163  0.090  0.017
    worst single field (sales)      15   0.167  0.167  0.062

0.167 is the ceiling, and it is reached by two sales lessons that share
procedural boilerplate ("security questionnaire" / "multiyear discount")
and nothing else. That same pair is also the overall maximum, so the
adversarial case -- two lessons in ONE domain, which share that domain's
vocabulary by construction -- is only marginally louder than the
cross-field maximum (0.158, a legal lesson against a sales one). That gap
being small is the evidence that excluding `tags`/`domain` from
`COMPARED_FIELDS` did the job it was meant to do; had grouping metadata
been in the comparison, same-field pairs would sit well above cross-field
ones instead.

The other side of the trade, from the same script: detection rate against
synthetic restatements (a lesson perturbed the way a second author
re-deriving it from another trace cluster plausibly writes it -- a dropped
caveat, reordered clauses, a fifth of the words gone):

    threshold  0.20  0.30  0.40  0.50  0.60  0.70  0.80
    recall     100%  100%   88%   73%   58%   35%   23%

So: 0.30 is the default. It sits ~1.8x above the highest similarity any
genuinely distinct pair reaches in either corpus, which is the margin that
makes a flagged pair worth a human's attention, and it is still the
highest value at which restatement recall is intact. 0.40 buys more margin
for 12 points of recall; 0.50 costs 27. Lower than 0.30 buys nothing --
the corpora are already silent at 0.20 -- while moving the bar toward the
0.167 noise ceiling.

Both numbers are from corpora of ~50 items. A store with thousands of
lessons in one narrow domain will have a higher noise ceiling than 0.167,
which is exactly why the threshold is a parameter on every entry point
here and a store-level setting in `memory/retrieval.json`, not a constant.

That default governs *reporting*. `dosage`'s injection-time suppression
defaults to OFF and is a separate, deliberately stricter setting -- see
`commontrace/dosage.py` for why dropping a lesson an agent would otherwise
have received is a decision a store makes rather than an upgrade side
effect.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from commontrace._lexical import STOPWORDS as _STOPWORDS
from commontrace._lexical import WORD_RE as _WORD_RE
from commontrace.overlap import DEFAULT_NUM_PERM, estimate_jaccard, minhash

#: The similarity at or above which two lessons are reported as saying the
#: same thing. See the module docstring for how this number was measured
#: (it is ~1.8x the highest similarity reached by any genuinely distinct
#: pair in either corpus that ships here, and the highest value at which
#: synthetic-restatement recall is still 100%).
DEFAULT_THRESHOLD = 0.30

#: Below this many items, all-pairs exact comparison is cheaper than
#: building MinHash signatures plus LSH buckets -- signing an item costs a
#: 128-permutation pass over its tokens, so banding only pays for itself
#: once the O(n^2) term dominates. 200 items is ~20k exact set comparisons,
#: measured at well under 100 ms; 6,400 items would be ~20M, which is where
#: banding becomes the difference between a usable command and a hung one.
LSH_MIN_ITEMS = 200

# Fields whose text an agent actually reads, and the weight each carries.
# `description` and `applies_when` are repeated in the comparable text
# rather than multiplied by a coefficient: Jaccard is over SETS, so the
# only way to weight a field is to decide whether its tokens are present.
# Repetition would not change a set, so there is deliberately no weighting
# here at all -- every listed field contributes its tokens once. Stated
# explicitly because `commontrace/retrieval.py` DOES weight its fields, and
# a reader comparing the two would otherwise assume an omission.
COMPARED_FIELDS = ("description", "applies_when", "do_not_apply_when")


def comparable_text(fm: dict, body: str = "") -> str:
    """The text two lessons are compared on. See the module docstring."""
    parts: list[str] = []
    for field in COMPARED_FIELDS:
        value = fm.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    if body and body.strip():
        parts.append(body)
    return "\n".join(parts)


def token_set(text: str) -> frozenset[str]:
    """Content tokens of `text`, stopwords and one-character words removed.

    Shares `commontrace/_lexical.py`'s stopword list and word pattern with
    retrieval rather than carrying its own copy, so a corpus this module
    calls redundant is redundant in the same vocabulary the retriever
    ranks in -- see that module for why drift between two copies of a
    stopword list is a real failure mode here and not a tidiness concern.
    """
    return frozenset(
        w for w in _WORD_RE.findall((text or "").lower())
        if w not in _STOPWORDS and len(w) > 1
    )


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Exact Jaccard similarity of two token sets. 0.0 if either is empty.

    An empty token set is treated as similar to nothing, including another
    empty one. The degenerate alternative (|∅ ∩ ∅| / |∅ ∪ ∅| as 1.0, or a
    ZeroDivisionError) would make every content-free lesson a duplicate of
    every other one -- the same failure `overlap.minhash` documents for its
    own empty-input case.
    """
    set_a = a if isinstance(a, (set, frozenset)) else set(a)
    set_b = b if isinstance(b, (set, frozenset)) else set(b)
    if not set_a or not set_b:
        return 0.0
    union = len(set_a | set_b)
    if union == 0:
        return 0.0
    return len(set_a & set_b) / union


@dataclass(frozen=True)
class Pair:
    """Two items that say substantially the same thing.

    `a` and `b` are ordered so `a` sorts first, making a report stable
    across runs and a pair comparable regardless of which side was
    discovered first.
    """

    a: str
    b: str
    similarity: float

    @classmethod
    def of(cls, left: str, right: str, similarity: float) -> "Pair":
        first, second = (left, right) if left <= right else (right, left)
        return cls(a=first, b=second, similarity=similarity)


def _bands_for(threshold: float, num_perm: int) -> tuple[int, int]:
    """(bands, rows) for LSH banding at `threshold`.

    A banded signature makes two items candidates when they agree on every
    row of at least one band, which happens with probability
    1-(1-s^r)^b for true similarity s -- an S-curve whose midpoint is
    approximately (1/b)^(1/r).

    The band count is chosen so that midpoint sits BELOW the threshold, at
    roughly 0.85 of it. Banding here is a candidate generator only: every
    surviving pair is re-checked with exact Jaccard in
    `find_near_duplicates`, so a midpoint that is too low costs time
    (more candidates to verify) while one that is too high costs recall
    (a genuine duplicate never becomes a candidate at all). Time is the
    cheaper mistake, so the curve is deliberately biased low.
    """
    target = max(0.05, min(0.95, threshold * 0.85))
    best: tuple[int, int] | None = None
    best_gap = math.inf
    for rows in range(1, num_perm + 1):
        if num_perm % rows:
            continue
        bands = num_perm // rows
        midpoint = (1.0 / bands) ** (1.0 / rows)
        # Prefer the configuration whose midpoint is closest to the target
        # from below; a midpoint above the target is only accepted if
        # nothing below it exists (rows == num_perm, bands == 1).
        gap = target - midpoint
        if gap < 0:
            continue
        if gap < best_gap:
            best_gap = gap
            best = (bands, rows)
    return best or (1, num_perm)


def find_near_duplicates(
    items: Sequence[tuple[str, str]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    num_perm: int = DEFAULT_NUM_PERM,
    similarity: Callable[[str, str], float] | None = None,
) -> list[Pair]:
    """Every pair in `items` whose similarity is at least `threshold`.

    `items` is (label, text). Labels need not be unique, but a duplicate
    label makes the resulting report ambiguous, so callers pass slugs.

    Returned most-similar first, then by label, so a report is stable.

    `similarity` overrides the comparison entirely -- pass one to drive
    this from the optional semantic layer (cosine over
    `memory/attention/index.npz`) while keeping this function's pairing,
    ordering and reporting. When it is supplied, the LSH fast path is
    skipped: banding is derived from MinHash of the lexical token set and
    says nothing about a caller's own metric, so using it to pre-filter
    would silently cap a semantic run's recall at the lexical one's.

    NOTE ON THE TWO TOKENIZERS. The LSH path signs text with
    `overlap.minhash`, which carries its own deliberately-pinned
    tokenizer (its signatures are persisted as `Trace.commons_signature`,
    so it cannot track `_lexical.py`); the verification pass uses
    `token_set`, which is `_lexical.py`'s. They agree on ordinary text and
    could in principle disagree on an edge case. That is safe here and
    only here: banding decides which pairs get LOOKED AT, and every pair
    that survives it is re-scored exactly, so a disagreement can cost
    recall on a corpus above `LSH_MIN_ITEMS` and can never report a pair
    the exact measure would not.
    """
    if threshold <= 0:
        raise ValueError("threshold must be positive; 0 would pair everything")
    labelled = [(str(label), text or "") for label, text in items]
    if len(labelled) < 2:
        return []

    if similarity is not None:
        pairs = [
            Pair.of(labelled[i][0], labelled[j][0], score)
            for i in range(len(labelled))
            for j in range(i + 1, len(labelled))
            if (score := similarity(labelled[i][1], labelled[j][1])) >= threshold
        ]
        return sorted(pairs, key=lambda p: (-p.similarity, p.a, p.b))

    tokens = [token_set(text) for _, text in labelled]

    if len(labelled) < LSH_MIN_ITEMS:
        candidates: Iterable[tuple[int, int]] = (
            (i, j) for i in range(len(labelled)) for j in range(i + 1, len(labelled))
        )
    else:
        bands, rows = _bands_for(threshold, num_perm)
        signatures = [minhash(text, num_perm=num_perm) for _, text in labelled]
        buckets: dict[tuple[int, tuple[int, ...]], list[int]] = defaultdict(list)
        for index, signature in enumerate(signatures):
            for band in range(bands):
                key = (band, tuple(signature[band * rows:(band + 1) * rows]))
                buckets[key].append(index)
        seen: set[tuple[int, int]] = set()
        for members in buckets.values():
            if len(members) < 2:
                continue
            for pos, i in enumerate(members):
                for j in members[pos + 1:]:
                    seen.add((i, j) if i < j else (j, i))
        candidates = sorted(seen)

    pairs = []
    for i, j in candidates:
        score = jaccard(tokens[i], tokens[j])
        if score >= threshold:
            pairs.append(Pair.of(labelled[i][0], labelled[j][0], score))
    return sorted(pairs, key=lambda p: (-p.similarity, p.a, p.b))


def closest(
    text: str,
    others: Sequence[tuple[str, str]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> Pair | None:
    """The item in `others` most similar to `text`, if any clears `threshold`.

    The one-against-many shape the authoring surfaces need: "is this lesson
    I am about to write already in the corpus". `Pair.a` is always the
    matched label and `Pair.b` is the empty string, because the new lesson
    has no slug yet -- callers report the match, not the pair.
    """
    tokens = token_set(text)
    if not tokens:
        return None
    best: Pair | None = None
    for label, other in others:
        score = jaccard(tokens, token_set(other or ""))
        if score >= threshold and (best is None or score > best.similarity):
            best = Pair(a=str(label), b="", similarity=score)
    return best


def estimated_similarity(sig_a: list[int], sig_b: list[int]) -> float:
    """MinHash estimate, for callers that already hold signatures.

    Re-exported from `commontrace/overlap.py` rather than reimplemented so
    there is one definition of "how similar are these signatures" in the
    codebase; `overlap.py` owns it because the fleet-signature exchange
    that cannot see the other side's text has no alternative.
    """
    return estimate_jaccard(sig_a, sig_b)
