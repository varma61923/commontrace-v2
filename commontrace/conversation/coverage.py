"""Conservative lexical evidence coverage, separate from answer correctness.

LongMemEval (https://arxiv.org/abs/2410.10813) evaluates abstention separately
from retrieval. A recalled person's name cannot establish an unrecorded
attribute. This gate requests deeper reading when subject evidence is absent;
it does not infer an answer or claim a calibrated probability.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from commontrace._lexical import WORD_RE, has_cjk, segment_cjk
from commontrace.conversation import profile

_WORDS = re.compile(r"[a-z0-9]+")
_QUESTIONS = frozenset("what when where which who whom whose why how did does tell know remember mention "
                       "mentioned said say ever".split())
_ATTRIBUTES = frozenset("name color colour type kind brand model date time day cost price age size height weight "
                        "amount number title phone email address city country state job car pet".split())
_QUALIFIERS = frozenset("name color colour type kind date time day amount number title".split())
# These identifiers must be mentioned explicitly, not inferred from a familiar
# person, object, timestamp or neighbouring passage. Ordinary semantic answer
# reasoning is left to the reader; uncertainty never deletes source evidence.
_IDENTIFIERS = frozenset("serial identifier registration license isbn sku".split())


@dataclass(frozen=True)
class EvidenceCoverage:
    confidence: float
    abstain: bool
    reason: str
    matched_terms: int
    query_terms: int
    subject_terms: tuple[str, ...]
    missing_subject_terms: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"confidence": self.confidence, "abstain": self.abstain, "reason": self.reason,
                "matched_terms": self.matched_terms, "query_terms": self.query_terms,
                "subject_terms": list(self.subject_terms), "missing_subject_terms": list(self.missing_subject_terms)}


def _terms(text: str) -> set[str]:
    # Preserve ASCII identifiers and underscore splitting exactly. Non-ASCII
    # words stay intact; CJK uses the same bigrams as the sparse retriever.
    words: set[str] = set()
    for word in WORD_RE.findall(text.lower()):
        if word.isascii():
            words.update(_WORDS.findall(word))
        else:
            words.update(segment_cjk(word) if has_cjk(word) else (word,))
    return words


def _covered(term: str, words: set[str]) -> bool:
    return term in words or term.isascii() and len(term) >= 5 and any(
        word.isascii() and len(word) >= 5 and word[:5] == term[:5] for word in words
    )


def assess(question: str, evidence: Sequence[str], *, labels: Iterable[str] = ()) -> EvidenceCoverage:
    """Assess only the returned evidence; no gold answers or corpus-wide lookup.

    Labels can credit an attributed speaker, but cannot satisfy subject or
    identifier evidence. Matching a long lexical stem supports inflections,
    not arbitrary substrings. Semantic matches without lexical coverage remain
    available to a deeper reader even when this conservative gate abstains.
    """
    asked = {word for word in _terms(question) if (len(word) > 2 or has_cjk(word))
             and word not in profile.STOPWORDS and word not in _QUESTIONS}
    entities = {word for name in profile.entities(question) for word in _terms(name)}
    subjects = asked - _ATTRIBUTES - entities
    if not subjects:
        subjects = asked - _QUALIFIERS - entities
    words = set().union(*(_terms(text) for text in evidence)) if evidence else set()
    missing = {word for word in subjects if not _covered(word, words)}
    if not asked or not evidence:
        reason = "no recalled evidence covers the question"
        return EvidenceCoverage(0.0, True, reason, 0, len(asked), tuple(sorted(subjects)), tuple(sorted(missing)))
    if (subjects and missing == subjects) or any(word not in words for word in asked & _IDENTIFIERS):
        reason = "recalled entities do not establish the requested subject or identifier"
        return EvidenceCoverage(0.0, True, reason, 0, len(asked), tuple(sorted(subjects)), tuple(sorted(missing)))
    label_words = {word for label in labels for word in _terms(label)}
    matched = max(sum(_covered(word, _terms(text) | label_words) for word in asked) for text in evidence)
    value = round(matched / len(asked), 3)
    return EvidenceCoverage(value, value == 0.0,
                            "lexical evidence covers part of the question; answer correctness is unverified",
                            matched, len(asked), tuple(sorted(subjects)), tuple(sorted(missing)))
