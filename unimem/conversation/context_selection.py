"""Bounded lexical facet diversity over already eligible, rendered evidence.

LongMemEval (https://arxiv.org/abs/2410.10813) separates retrieval from reading
and includes multi-session synthesis and abstention. Mem0's additive pipeline
preserves historical facts and fuses keyword, dense and entity signals; its
retrieval still needs a finite context budget. This original selector operates
after those signals, never rewriting stored history or generating a summary.

Callers supply actual excerpts and complete atomic graph-support group costs,
after scope/time/injection checks. The requested first eligible ranked anchors
stay first. Later candidates earn priority only for additional query lexical facets
per quoted token. Unselected candidates remain in their original relative rank.
Facet coverage is a lexical heuristic, not entailment, answer correctness or a
calibrated probability. The selector neither reads sources nor authorizes them.
"""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass

from unimem.conversation import profile
from unimem.conversation.coverage import _QUESTIONS, _terms

MAX_CANDIDATES = 200
MAX_PASSAGES = 3
MAX_PASSAGE_CHARS = 65536
MAX_QUESTION_CHARS = 16384
MAX_FACETS = 256
MAX_TOKEN_COST = 1_000_000


@dataclass(frozen=True, slots=True)
class Candidate:
    """One already eligible evidence group, with source bodies exactly rendered.

    ``passages`` exclude speaker/session labels and include any graph ancestors.
    ``token_cost`` includes labels, headers and required ancestor excerpts. It
    can conservatively double-count overhead shared with other candidates;
    actual admission into the budget belongs to the authoritative assembler.
    """

    turn: int
    passages: tuple[str, ...]
    token_cost: int
    session: str = ""
    at: dt.datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session, str) or len(self.session) > 4096:
            raise ValueError("candidate session must be a bounded string")
        if self.at is not None and (not isinstance(self.at, dt.datetime) or self.at.tzinfo is not None):
            raise ValueError("candidate time must be a naive canonical datetime")
        if isinstance(self.turn, bool) or not isinstance(self.turn, int) or self.turn < 1:
            raise ValueError("candidate turn must be a positive integer")
        if not isinstance(self.passages, tuple) or not 1 <= len(self.passages) <= MAX_PASSAGES \
                or any(not isinstance(passage, str) for passage in self.passages):
            raise ValueError(f"candidate passages must contain 1..{MAX_PASSAGES} rendered strings")
        if sum(len(passage) for passage in self.passages) > MAX_PASSAGE_CHARS:
            raise ValueError("candidate rendered passages exceed the character limit")
        if isinstance(self.token_cost, bool) or not isinstance(self.token_cost, int) \
                or not 1 <= self.token_cost <= MAX_TOKEN_COST:
            raise ValueError(f"candidate token cost must be an integer in 1..{MAX_TOKEN_COST}")


@dataclass(frozen=True, slots=True)
class SelectionPlan:
    order: tuple[int, ...]
    priority: tuple[int, ...]
    facets: tuple[str, ...]
    covered_facets: tuple[str, ...]
    missing_facets: tuple[str, ...]
    omitted_facets: int
    quoted_tokens: int
    strategy: str = "coverage-v1"
    mode: str = "detail"
    sessions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {"strategy": self.strategy, "order": list(self.order), "priority": list(self.priority),
                "facets": list(self.facets), "covered_facets": list(self.covered_facets),
                "missing_facets": list(self.missing_facets), "omitted_facets": self.omitted_facets,
                "quoted_tokens": self.quoted_tokens, "mode": self.mode, "sessions": list(self.sessions),
                "meaning": "candidate excerpt lexical diversity; final emitted coverage is assessed separately"}


def _facets(question: str) -> tuple[tuple[str, ...], int]:
    words = sorted(word for word in _terms(question)
                   if word not in profile.STOPWORDS and word not in _QUESTIONS)
    return tuple(words[:MAX_FACETS]), max(0, len(words) - MAX_FACETS)


def _supported(facets: tuple[str, ...], passages: tuple[str, ...]) -> frozenset[str]:
    words = set().union(*(_terms(passage) for passage in passages))
    prefixes = {word[:5] for word in words if word.isascii() and len(word) >= 5}
    # Same conservative long-prefix heuristic as the evidence coverage gate;
    # short identifiers require an exact match and CJK terms remain Unicode.
    return frozenset(word for word in facets if word in words
                     or word.isascii() and len(word) >= 5 and word[:5] in prefixes)


def prioritize(question: str, candidates: Sequence[Candidate], *, anchor_count: int = 1) -> SelectionPlan:
    """Return a deterministic, complete permutation of at most 200 candidates.

    This does not admit a passage into the final budget. The caller preserves
    the rest of the full ranking and reruns its normal atomic evidence-group
    admission. ``anchor_count`` preserves the caller's existing top-hit quota
    before introducing additional facet diversity; the pure default keeps one.
    Integer cross-products compare marginal facets/token without
    floating-point instability; original rank resolves equal utility. Work is
    bounded by 200 candidates, 256 facets and bounded excerpt lengths.
    """
    if not isinstance(question, str) or len(question) > MAX_QUESTION_CHARS:
        raise ValueError("question must be a bounded string")
    if isinstance(anchor_count, bool) or not isinstance(anchor_count, int) or not 0 <= anchor_count <= MAX_CANDIDATES:
        raise ValueError(f"anchor_count must be an integer in 0..{MAX_CANDIDATES}")
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence) \
            or len(candidates) > MAX_CANDIDATES \
            or any(not isinstance(candidate, Candidate) for candidate in candidates):
        raise ValueError(f"candidates must be a sequence of at most {MAX_CANDIDATES} Candidate objects")
    if len({candidate.turn for candidate in candidates}) != len(candidates):
        raise ValueError("candidate turn identities must be unique")
    facets, omitted = _facets(question)
    if not candidates:
        return SelectionPlan((), (), facets, (), facets, omitted, 0)
    if not facets:
        return SelectionPlan(tuple(candidate.turn for candidate in candidates), (), (), (), (), omitted, 0)
    support = [_supported(facets, candidate.passages) for candidate in candidates]
    count = min(anchor_count, len(candidates))
    chosen = list(range(count))
    remaining = list(range(count, len(candidates)))
    covered = {facet for index in chosen for facet in support[index]}
    while remaining:
        best: int | None = None
        best_gain = 0
        for index in remaining:
            gain = len(support[index] - covered)
            if not gain:
                continue
            if best is None or gain * candidates[best].token_cost > best_gain * candidates[index].token_cost:
                best, best_gain = index, gain
        if best is None:
            break
        chosen.append(best)
        covered.update(support[best])
        remaining.remove(best)
    priority = tuple(candidates[index].turn for index in chosen)
    return SelectionPlan(priority + tuple(candidates[index].turn for index in remaining), priority, facets,
                         tuple(sorted(covered)), tuple(word for word in facets if word not in covered), omitted,
                         sum(candidates[index].token_cost for index in chosen))


_HISTORY = re.compile(
    r"\b(?:summar\w*|recap|overview|timeline|chronolog\w*|sequence|order|over time|"
    r"across .*?(?:sessions?|conversations?|chats?)|all (?:the )?(?:events?|changes?|times?|steps?)|"
    r"how .*?chang\w*|before and after|used to|earlier .*?(?:now|later)|"
    r"contradict\w*|conflict\w*|compare|comparison)\b", re.I)


def history_question(question: str) -> bool:
    """Query-shape hint only; no benchmark labels or assumed answers."""
    return bool(_HISTORY.search(question[:MAX_QUESTION_CHARS]))


def prioritize_history(question: str, candidates: Sequence[Candidate], *, anchor_count: int = 1) -> SelectionPlan:
    """Retain relevant session/time diversity after lexical facets are satisfied.

    Narrow queries use the existing facet strategy. History queries reserve one
    ranked anchor, then reward novel facets, sessions and chronological span.
    A history reward requires a lexical match in the rendered source body;
    unrelated sessions never earn diversity solely from their metadata.
    This is candidate ordering only: normal atomic evidence and budget admission
    remain authoritative. No facts are inferred, merged or superseded here.
    """
    base = prioritize(question, candidates, anchor_count=anchor_count)
    if not history_question(question) or not candidates or not base.facets:
        from dataclasses import replace

        return replace(base, strategy="coverage-v2")
    support = [_supported(base.facets, row.passages) for row in candidates]
    chosen = list(range(min(1, anchor_count, len(candidates))))
    remaining = list(range(len(chosen), len(candidates)))
    covered = {facet for index in chosen for facet in support[index]}
    sessions = {candidates[index].session for index in chosen if candidates[index].session}
    dates = [moment for index in chosen if (moment := candidates[index].at) is not None]
    while remaining:
        lo, hi = (min(dates), max(dates)) if dates else (None, None)
        best: int | None = None
        best_gain = 0
        for index in remaining:
            row = candidates[index]
            if not support[index]:
                continue
            gain = 4 * len(support[index] - covered)
            gain += 2 * bool(row.session and row.session not in sessions)
            gain += bool(row.at is not None and (lo is None or hi is None or row.at < lo or row.at > hi))
            if gain and (best is None or gain * candidates[best].token_cost > best_gain * row.token_cost):
                best, best_gain = index, gain
        if best is None:
            break
        chosen.append(best)
        row = candidates[best]
        covered.update(support[best])
        if row.session:
            sessions.add(row.session)
        if row.at is not None:
            dates.append(row.at)
        remaining.remove(best)
    priority = tuple(candidates[index].turn for index in chosen)
    return SelectionPlan(priority + tuple(candidates[index].turn for index in remaining), priority, base.facets,
                         tuple(sorted(covered)), tuple(word for word in base.facets if word not in covered),
                         base.omitted_facets, sum(candidates[index].token_cost for index in chosen),
                         "coverage-v2", "history", tuple(sorted(sessions)))
