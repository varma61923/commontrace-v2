"""The gates a drafted lesson must pass before it can be activated, as one function."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from commontrace import memory_guard, redundancy, templates
from commontrace.commands.lesson_cmd import _guard_fields


def gate_failures(fm: dict[str, Any], body: str, active_texts: list[tuple[str, str]]) -> list[str]:
    failed = []
    if templates.unfilled_placeholders(fm, body):
        failed.append("scaffolding")
    if memory_guard.scan_fields(_guard_fields(fm, body)).should_block:
        failed.append("safety")
    if redundancy.closest(redundancy.comparable_text(fm, body), active_texts,
                          threshold=redundancy.DEFAULT_THRESHOLD) is not None:
        failed.append("redundancy")
    return failed


_RULE_SECTION = re.compile(r"^##\s+Rule\s*\n(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)


def candidate_text(fm: Mapping[str, object], body: str) -> str:
    """Compare the rule and activation context without repeated boilerplate.

    Evidence/counter-condition templates shared by unrelated proposals should
    not themselves make two candidates duplicates.
    """
    rule = _RULE_SECTION.search(body)
    parts = [fm.get("description"), fm.get("applies_when"), rule.group(1) if rule else body]
    return "\n".join(part.strip() for part in parts if isinstance(part, str) and part.strip())[:20_000]


@dataclass(frozen=True)
class CandidateDuplicate:
    identity: str
    similarity: float
    method: str


def find_candidate_duplicate(
    text: str, existing: Sequence[tuple[str, str]], *,
    semantic_dedup: bool = False, semantic_threshold: float = 0.9,
) -> CandidateDuplicate | None:
    """Find duplicate *proposals*, retaining every underlying source trace.

    The optional encoder runs locally. Without that model stack, exact lexical
    comparison still operates. This is admission screening, not a declaration
    that semantically similar rules are interchangeable or safe to activate.
    """
    if isinstance(semantic_threshold, bool) or not math.isfinite(semantic_threshold) \
            or not 0 < semantic_threshold <= 1:
        raise ValueError("semantic_threshold must be finite and in (0, 1]")
    duplicate = redundancy.closest(text, existing, threshold=0.8)
    if duplicate is not None:
        return CandidateDuplicate(duplicate.a, duplicate.similarity, "lexical")
    if not semantic_dedup or not existing:
        return None
    from commontrace import semantic

    if not semantic.available():
        return None
    # Bound each inference batch and score matrix independently of corpus size.
    for start in range(0, len(existing), 128):
        batch = existing[start:start + 128]
        try:
            match = semantic.best_matches([text], [value[:20_000] for _name, value in batch],
                                          threshold=semantic_threshold, top_k=1)[0]
        except semantic.SemanticUnavailable:
            return None
        if match["matches"]:
            index, similarity = match["matches"][0]
            return CandidateDuplicate(batch[index][0], similarity, "semantic")
    return None
