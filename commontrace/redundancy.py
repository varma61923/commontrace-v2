"""Which lessons say the same thing -- near-duplicate detection, no embeddings."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from commontrace._lexical import STOPWORDS as _STOPWORDS
from commontrace._lexical import WORD_RE as _WORD_RE
from commontrace.overlap import DEFAULT_NUM_PERM, estimate_jaccard, minhash

DEFAULT_THRESHOLD = 0.30

LSH_MIN_ITEMS = 200

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
    """Content tokens of `text`, stopwords and one-character words removed."""
    return frozenset(
        w for w in _WORD_RE.findall((text or "").lower())
        if w not in _STOPWORDS and len(w) > 1
    )


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Exact Jaccard similarity of two token sets. 0.0 if either is empty."""
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
    """Two items that say substantially the same thing."""

    a: str
    b: str
    similarity: float

    @classmethod
    def of(cls, left: str, right: str, similarity: float) -> "Pair":
        first, second = (left, right) if left <= right else (right, left)
        return cls(a=first, b=second, similarity=similarity)


def _bands_for(threshold: float, num_perm: int) -> tuple[int, int]:
    target = max(0.05, min(0.95, threshold * 0.85))
    best: tuple[int, int] | None = None
    best_gap = math.inf
    for rows in range(1, num_perm + 1):
        if num_perm % rows:
            continue
        bands = num_perm // rows
        midpoint = (1.0 / bands) ** (1.0 / rows)
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
    """Every pair in `items` whose similarity is at least `threshold`."""
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
    """The item in `others` most similar to `text`, if any clears `threshold`."""
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
    """MinHash estimate, for callers that already hold signatures."""
    return estimate_jaccard(sig_a, sig_b)
