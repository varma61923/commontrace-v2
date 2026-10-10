"""Contradictions between facts, found when a fact is written.

Two statements form a candidate pair when their content words are the same
once numbers and negations are set aside ("Alice likes spicy food" against
"Alice does not like spicy food"; "Bob has 2 cats" against "Bob has 3 cats").
A pair is classified as:

- ``negation``: only a negation differs. Deterministic and high precision.
- ``value``: only the numbers differ. Numbers are often identifiers ("customer
  5" and "customer 6" are different customers, not a contradiction), so this
  is never acted on without a judge.
- ``judged``: an optional LLM judge (``COMMONTRACE_FACT_CONFLICT_JUDGE=llm``)
  confirmed that the newer statement replaces the older one. With a fact
  embedder configured, the judge also sees semantically similar facts that
  share no wording.

``COMMONTRACE_FACT_CONFLICTS`` decides what happens: ``flag`` (default)
records the pair for review and changes nothing; ``supersede`` lets a
negation or judged contradiction close the older fact's validity window (the
Graphiti rule, through the same temporal guard as `resolve_contradiction`);
``off`` disables detection. Value pairs are always flagged only.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass

MODES = ("off", "flag", "supersede")
_STEM = re.compile(r"(?:ing|ed)$")


@dataclass(frozen=True)
class Conflict:
    older: str
    newer: str
    kind: str  # negation | value | judged
    older_statement: str
    newer_statement: str
    detected_at: str
    action: str = "flagged"  # flagged | superseded
    reason: str = ""


def mode() -> str:
    value = os.environ.get("COMMONTRACE_FACT_CONFLICTS", "flag").strip().lower() or "flag"
    if value not in MODES:
        raise ValueError("COMMONTRACE_FACT_CONFLICTS must be off, flag or supersede")
    return value


def _stem(word: str) -> str:
    """One stem per inflection: like, likes, liked and liking all map to ``lik``."""
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        word = word[:-1]
    match = _STEM.search(word)
    if match and len(word) - len(match.group()) >= 2:
        word = word[:match.start()]
    if word.endswith("e") and len(word) >= 3:
        word = word[:-1]
    return word


def slot_key(statement: str) -> frozenset:
    """Stemmed content words without numbers or negations: equal keys are candidate pairs."""
    from commontrace import hierarchical

    words, numbers, negations = hierarchical._signature(statement)
    content = hierarchical._content(words) - negations - set(numbers)
    return frozenset(_stem(w) for w in content if not w.isdigit())


def classify(older: str, newer: str) -> str | None:
    """``negation``, ``value`` or None (not a contradiction this rule can see)."""
    from commontrace import hierarchical

    if hierarchical._normalize_statement(older) == hierarchical._normalize_statement(newer):
        return None
    key = slot_key(older)
    if len(key) < 2 or key != slot_key(newer):
        return None
    _wa, numbers_a, negations_a = hierarchical._signature(older)
    _wb, numbers_b, negations_b = hierarchical._signature(newer)
    if bool(negations_a) != bool(negations_b) and numbers_a == numbers_b:
        return "negation"
    if numbers_a != numbers_b and bool(negations_a) == bool(negations_b) and numbers_a and numbers_b:
        return "value"
    return None


_JUDGE_PROMPT = """You compare a NEW fact with EXISTING facts from the same memory.
For each existing fact, decide whether the NEW fact replaces it: they describe the
same subject and attribute and cannot both be true now (for example a changed
employer, address, preference or count). Facts about different subjects, or that
can both be true, are not replaced.

NEW: {new}
EXISTING:
{existing}

Answer with JSON only: {{"replaces": ["<id>", ...]}} listing only ids shown above."""


def judge(newer: str, candidates: list, complete=None) -> set[str]:
    """Ids among `candidates` (facts) that an LLM judges the newer statement to replace.

    Defensive: only ids that were offered are accepted, and any failure means none.
    """
    if not candidates:
        return set()
    from commontrace import llm

    offered = {f.id: f.statement for f in candidates[:8]}
    prompt = _JUDGE_PROMPT.format(new=newer, existing="\n".join(f"- {i}: {s}" for i, s in offered.items()))
    try:
        text = complete(prompt) if complete is not None else llm.complete(prompt)[0]
        ids = llm._extract_json_object(str(text)).get("replaces", [])
    except Exception:  # noqa: BLE001 - a judge that fails judges nothing
        return set()
    return {i for i in ids if isinstance(i, str) and i in offered}


def _queue_path(root: str) -> str:
    from commontrace import paths

    return os.path.join(paths.memory_dir(root), "facts", "conflicts.jsonl")


def record(root: str, conflicts: list[Conflict]) -> None:
    if not conflicts:
        return
    from commontrace import holdout_io

    path = _queue_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    holdout_io._append_lines(path, [json.dumps(asdict(c), sort_keys=True) for c in conflicts])


def pending(root: str) -> list[Conflict]:
    """Recorded contradictions whose older fact is still active (newest first)."""
    from commontrace import hierarchical

    path = _queue_path(root)
    if not os.path.isfile(path):
        return []
    facts = hierarchical.load_facts(root)
    seen, out = set(), []
    with open(path, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    for row in reversed(rows):
        key = (row["older"], row["newer"])
        if key in seen:
            continue
        seen.add(key)
        older, newer = facts.get(row["older"]), facts.get(row["newer"])
        if older is not None and newer is not None and older.status == "active" and newer.status == "active":
            out.append(Conflict(**row))
    return out
