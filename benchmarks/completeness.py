"""Deterministic lexical completeness grading for benchmark contexts.

The existing `complete` column in conversation_bench is evidence-id based: it
asks whether the gold evidence turn *refs* survived into the assembled context.
This grader asks the complementary lexical question on the retrieved context
itself: does the context actually contain the content of the gold evidence?

It is deterministic and model-free: no embeddings, no LLM, same inputs in,
same bucket out.

Buckets:
  COMPLETE     -- every evidence element is present in the context
  PARTIAL      -- some elements are present, some are missing
  INSUFFICIENT -- no element is present
Each element is reported under `present` or `missing` by the same key.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping

COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
INSUFFICIENT = "INSUFFICIENT"

# Words that carry no evidence on their own; they never count towards presence.
_STOPWORDS = frozenset(
    "a an the and or but if then else when while of to in on for with without by at from as is are was were "
    "be been being do does did has have had it its this that these those i you he she we they them his her our "
    "their my your not no yes so such than too very can could will would should may might must about into over".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# An evidence element counts as present when at least this fraction of its
# distinctive tokens appears in the context (lexical recall per element).
PRESENCE_THRESHOLD = 0.5


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _key_tokens(text: str) -> list[str]:
    seen: list[str] = []
    for tok in _tokens(text or ""):
        if len(tok) >= 4 and tok not in _STOPWORDS and tok not in seen:
            seen.append(tok)
    return seen


def grade_context_completeness(
    context: str,
    evidence: Mapping[str, str] | None,
    *,
    answer: str = "",
    threshold: float = PRESENCE_THRESHOLD,
) -> dict:
    """Grade how completely `context` covers the gold evidence.

    `evidence` maps an evidence element id to its source text (typically the
    gold turn's text). An element is "present" when at least `threshold` of its
    distinctive tokens appear in the context. With no evidence texts but a gold
    answer, the answer's own distinctive tokens become the elements. With
    nothing checkable, the bucket is None and the score is None.
    """
    ctx_tokens = set(_tokens(context or ""))
    elements: dict[str, bool] = {}
    for eid, text in (evidence or {}).items():
        keys = _key_tokens(text)
        if not keys:
            continue
        hits = sum(1 for k in keys if k in ctx_tokens)
        elements[str(eid)] = (hits / len(keys)) >= threshold
    if not elements and answer:
        for key in _key_tokens(answer):
            elements[key] = key in ctx_tokens
    if not elements:
        return {"bucket": None, "score": None, "present": [], "missing": []}
    present = [eid for eid, ok in elements.items() if ok]
    missing = [eid for eid, ok in elements.items() if not ok]
    score = round(len(present) / len(elements), 4)
    if missing and present:
        bucket = PARTIAL
    elif missing:
        bucket = INSUFFICIENT
    else:
        bucket = COMPLETE
    return {"bucket": bucket, "score": score, "present": present, "missing": missing}


def bucket_counts(buckets: Iterable[str | None]) -> dict[str, float | None]:
    """Fraction of non-None rows in each bucket plus a grader score helper."""
    vals = [b for b in buckets if b in (COMPLETE, PARTIAL, INSUFFICIENT)]
    if not vals:
        return {"complete": None, "partial": None, "insufficient": None}
    n = len(vals)
    return {
        "complete": round(vals.count(COMPLETE) / n, 4),
        "partial": round(vals.count(PARTIAL) / n, 4),
        "insufficient": round(vals.count(INSUFFICIENT) / n, 4),
    }
