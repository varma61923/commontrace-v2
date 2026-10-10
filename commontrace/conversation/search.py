"""Recall: the turns that answer a question, assembled into a dated, token-budgeted
context. Lexical and semantic arms are fused per sub-query and combined by their
best rank (a turn that is first for one facet keeps that), a question's time
window lifts the turns said or set in it, and the page is filled best-first with
each hit's neighbouring turns, then shown in the order things were said."""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
import math
import re
import threading
import unicodedata
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import chain

from commontrace import injection_guard
from commontrace._lexical import WORD_RE, has_cjk, segment_cjk
from commontrace.conversation import profile, timeparse, unicode_index
from commontrace.conversation.store import (  # noqa: F401 - re-exported (canonical home: store)
    ConversationError,
    Store,
    Turn,
    sigmoid_bm25,
)

DEFAULT_BUDGET = 1500
RRF_K = 60
POOL = 200


@dataclass
class Options:
    budget: int = DEFAULT_BUDGET
    pool: int = POOL
    neighbours_before: int = 2
    neighbours_after: int = 2
    neighbour_hits: int | None = None
    neighbour_minutes: float | None = 60.0
    # Fill the gap between a hit and an already-chosen turn of the same session
    # when at most this many turns lie between them ("steps 20 to 23" retrieves
    # 20 and 23; the span in between is the context). 0 turns it off.
    bridge_turns: int = 0
    excerpt_tokens: int | None = None  # longest a single turn may show; default budget/5, at least 200
    window_boost: float = 1.0
    entity_boost: float = 0.1
    lexical_weight: float = 0.5
    rerank: str | None = "auto"
    rerank_depth: int = 50
    rerank_blend: float = 1.0  # the cross-encoder's weight beside the fused rank; 0 lets it replace that rank
    profile_facts: int = 4
    instructions: int = 6
    broad: bool | None = None  # None: detect summary / ordering / across-session questions
    recency_boost: float = 0.3
    recency_pool: int = 4  # "current/latest" questions: how many top matches compete on time
    primary_hits: int | None = None  # best hits placed before any neighbours (default 3)
    embedder: str | None = "auto"
    summaries: bool = True
    sessions: tuple[str, ...] = ()
    speakers: tuple[str, ...] = ()
    since: str | None = None
    until: str | None = None
    graph_hops: int | None = None  # None: adapt to relational clauses; 0 disables; maximum 2
    adaptive_budget: bool = False  # True: scale `budget` by question shape (see budget_for), up to max_budget
    max_budget: int = 12_000
    context_strategy: str = "legacy"  # coverage-v1 prioritizes marginal excerpt facets per quoted token
    # True: when the delivered context leaves budget unused and misses some of the
    # question's subject terms, search facet queries for those terms, add what they
    # find after the first-pass ranking and assemble again (explain["second_pass"]).
    second_pass: bool = False


@dataclass
class Recall:
    question: str
    context: str
    tokens: int
    turns: list[int]
    ranked: list[int]
    window: tuple[str, str, str] | None
    explain: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"question": self.question, "context": self.context, "tokens": self.tokens,
                "turns": self.turns, "window": self.window, "explain": self.explain}


@dataclass(frozen=True)
class _EmittedText:
    """Body bytes actually delivered, with only their displayed attribution."""

    body: str
    speaker: str = ""


@dataclass(frozen=True)
class DenseCandidates:
    """Externally prefetched unit IDs bound to an exact canonical revision.

    Providers supply IDs only; source text and eligibility always come from
    this Store. A concurrent canonical write rejects the stale arm rather than
    injecting vector results against a different evidence snapshot.
    """

    identity: object
    stamp: tuple[object, ...]
    model: str
    rankings: Mapping[str, tuple[int, ...]]


def tokens(text: str) -> int:
    """The context's size in tokens, estimated the way most tokenizers land: ~4 chars each."""
    return max(1, math.ceil(len(text) / 4)) if text else 0


def _context_strategy(value: object) -> str:
    if not isinstance(value, str) or value not in ("legacy", "coverage-v1"):
        raise ConversationError("context_strategy must be legacy or coverage-v1")
    return value


_BROAD = re.compile(
    r"\b(?:summar(?:y|ise|ize|ies)|overview|recap|progress(?:ed)?|evolv(?:e|ed)|over time|so far|timeline|"
    r"in (?:what|which) order|order in which|sequence|chronolog\w*|throughout|across (?:our|my|all|the|these|"
    r"different) (?:conversations?|sessions?|chats?|discussions?|requests?)|walk me through|history of|"
    r"all (?:the )?(?:times|things|steps|changes|features|issues)|every (?:time|change|step)|"
    r"how many (?!(?:days|weeks|months|years|hours|minutes)\b)|"
    r"total (?:number|count|amount)|in total|altogether|combined|list (?:all|every))\b", re.I)


_ABOUT_ASSISTANT = re.compile(r"\b(?:you (?:said|told|suggested|recommended|mentioned|gave|listed|provided|wrote|"
                              r"explained|shared|described|came up with)|your (?:suggestion|recommendation|answer|"
                              r"advice|list)|our (?:previous |last |earlier )?(?:chat|conversation))\b", re.I)
_SUMMARY = re.compile(r"\b(?:summar(?:y|ise|ize|ies)|overview|recap)\b", re.I)


_CURRENT = re.compile(
    r"\b(?:current(?:ly)?|now|latest|most recent(?:ly)?|these days|still|anymore|any more|"
    r"updated?|today|at the moment|right now|nowadays|"
    r"where (?:do|does|am|is|are)\b|what (?:is|are)\b|who (?:is|are)\b)\b",
    re.I,
)

_ASKS_WHEN = re.compile(
    r"\b(?:when|what (?:date|day|month|year)|how long (?:ago|since|before|after|had|have|did|was|were)|"
    r"how many (?:days|weeks|months|years) (?:ago|since|before|after|between|had|have|passed|did|was|were|"
    r"until|from))\b",
    re.I,
)

_PREFERENCE = re.compile(
    r"\b(?:prefer|preference|like|favorite|favourite|enjoy|love|hate|dislike|usual|typically|normally|fond of|"
    r"drink|eat|drive|use|cook|steak|coffee|seat|font|slide)\b",
    re.I,
)

_RELATIONAL = re.compile(r"\b(?:whose|(?:my|the) (?:person|friend|colleague|company|project|team) (?:who|that)|"
                         r"(?:friend|colleague|manager|partner|owner|author|founder)(?:'s| of)|"
                         r"(?:connected|related|associated) (?:to|with))\b", re.I)


def _graph_candidates(store: Store, question: str, ranked: list[int], allowed: set[int] | None,
                      hops: int) -> tuple[list[int], list[dict]]:
    """Bounded evidence traversal over indexed entity co-occurrences.

    This discovers candidates, not inferred facts. Exact source turns must still
    pass the ordinary evidence screen, reranker and context budget.
    """
    seeds = ranked[:6]
    visited = set(seeds)
    seen_entities = set(profile.entities(question))
    candidates, paths = [], []
    for depth in range(min(2, max(0, hops))):
        source_turns = store.turns(seeds)
        seeds = [t for t in seeds if t in source_turns and not _flagged(source_turns[t])]
        if not seeds:
            break
        entities = list(store.db.execute(
            "SELECT name, turn FROM entities WHERE turn IN (SELECT value FROM json_each(?)) "
            "ORDER BY name, turn LIMIT 96",
            (json.dumps(seeds),)))
        origins: dict[str, int] = {}
        for name, turn in entities:
            if name not in seen_entities and len(origins) < 16:
                origins.setdefault(name, turn)
        seen_entities.update(origins)
        links = store.entity_turns(origins, max_matches=32)
        next_seeds = []
        for name, turns in links.items():
            for turn in sorted(turns):
                if turn in visited or (allowed is not None and turn not in allowed):
                    continue
                visited.add(turn)
                next_seeds.append(turn)
                paths.append({"source": origins[name], "entity": name, "turn": turn, "hop": depth + 1})
                if len(paths) >= 64:
                    break
            if len(paths) >= 64:
                break
        fetched = store.turns(next_seeds)
        seeds = [t for t in next_seeds if t in fetched and not _flagged(fetched[t])]
        candidates.extend(seeds)
        if len(paths) >= 64:
            break
    safe = set(candidates)
    return candidates, [p for p in paths if p["turn"] in safe]


def asks_current(question: str) -> bool:
    """A question about how things stand now: a later statement should outrank an older one."""
    return bool(_CURRENT.search(question or ""))


_QUESTION_WORDS = frozenset("what when where which who whom whose why how did does tell know remember mention "
                            "mentioned said say ever".split())

_ATTRIBUTE_WORDS = frozenset(
    "name color colour type kind brand model date time day cost price age size height weight amount number title "
    "phone email address city country state job car pet".split()
)


def confidence(store: Store, question: str, turn_ids: list[int]) -> float:
    """Lexical evidence coverage (0..1), never an answer probability."""
    from commontrace.conversation.coverage import assess

    turns = store.turns(turn_ids)
    return assess(question, [turn.annotated() for turn in turns.values()],
                  labels=[turn.speaker for turn in turns.values()]).confidence


_REFERS_BACK = re.compile(r"\b(?:remind me|you (?:said|mentioned|told|suggested|recommended|gave|listed|explained|"
                          r"provided|shared)|(?:our|the) (?:previous|earlier|last) (?:chat|conversation|discussion))\b",
                          re.I)
SUMMARY_EXCERPT = 40  # the passage per turn a summary question reads
ORDERING_TURNS = 2000  # user turns an ordering question may list
ORDERING_EXCERPT = 24  # the shortest passage per turn when they do not all fit
_COUNTING = re.compile(r"\bhow many (?:different |distinct |unique |separate )?(?!(?:days|weeks|months|years|hours|"
                       r"minutes)\b)", re.I)
_ORDERING = re.compile(r"\b(?:in (?:what|which) order|order in which|sequence|chronolog\w*|timeline)\b", re.I)


def is_broad(question: str) -> bool:
    """A question about a whole topic across sessions (a summary, an order of events, a
    count across conversations): it needs coverage more than the single best passage."""
    question = question or ""
    if _REFERS_BACK.search(question) and not _SUMMARY.search(question) and not _ORDERING.search(question):
        return False  # "remind me what you said about X": one earlier answer, not the whole history
    return bool(_BROAD.search(question)) or bool(gap_events(question))


_ADVICE = re.compile(r"\b(?:recommend|suggest|suggestions?|ideas?|tips?|advice|should I|what should|"
                     r"any (?:good|other)|help me|how (?:can|could|should|would|do) I|how to|walk me through|"
                     r"can you (?:show|explain|help|give|write|create|make|plan)|explain|steps?|approach|"
                     r"what (?:libraries|tools|options|places|ways)|best way|plan(?:ning)? (?:my|a|the))\b", re.I)


_ABOUT_SELF = re.compile(r"\b(?:profession|occupation|job|career|for a living|my (?:work|role|background|name|age)|"
                         r"who am I|where (?:do|did) I (?:live|work|grow up)|how old am I)\b", re.I)


def _stems(text: str) -> set[str]:
    if unicode_index.relevant(text):
        return {w if has_cjk(w) else w[:5] for w in _unicode_words(text)
                if (len(w) > 2 or has_cjk(w)) and w not in profile.STOPWORDS}
    return {w[:5] for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in profile.STOPWORDS}


_FRAME_WORDS = frozenset("summary summarise summarize summarizing comprehensive overview recap progress progressed "
                         "evolved evolve provide give describe including include detailed brief please".split())


_QUERY_FRAME = re.compile(
    r"^(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?"
    r"(?:give\s+me|tell\s+me|show\s+me|provide\s+me|provide|give|list|tell|show|describe|explain|recap|name|summarize|summarise)?\s*"
    r"(?:a\s+|an\s+|the\s+)?(?:comprehensive\s+|detailed\s+|brief\s+|full\s+|complete\s+|clear\s+)?\s*"
    r"(?:summary|overview|recap|breakdown|progression|evolution|timeline|history|account|details?|list)?\s*"
    r"(?:of\s+)?\s*"
    r"(?:how\s+(?:i|we|my|our)\s+(?:handled|progressed|developed|built|managed|worked\s+on|approached|dealt\s+with|solved|resolved|improved|understood)|"
    r"what\s+(?:i|we)\s+(?:did|discussed|talked\s+about|learned)|"
    r"how\s+(?:my|our)\s+(?:understanding|project|work|application)\s+(?:of\s+|and\s+|developed|progressed|evolved|improved)*|"
    r"everything\s+(?:we\'ve|we\s+have|i\'ve|i\s+have)\s+(?:covered|discussed|worked\s+on|talked\s+about)\s+(?:about\s+)?|"
    r"what (?:is|was|were|are)|list|order|how many|in what order|order in which|"
    r"walk me through|do you remember|did i (?:ever )?mention|"
    r"how\s+)?\s*",
    re.I,
)
_QUERY_TRAILING = re.compile(
    r"(?:,\s*in order|\bin order\b|\bthroughout our conversations?\b|\bacross all (?:our )?conversations?\b|"
    r"\bacross our discussions?\b|\bfrom our discussions?\b|"
    r"\bmention only\b.*|\bonly and only\b.*|\bso far\b|\bover time\b|\bfrom start to finish\b).*$",
    re.I,
)


_CORE_STRIP_ARTICLE = re.compile(r"^(?:the\s+|about\s+|and\s+|my\s+|our\s+|how\s+)+", re.I)
_CORE_STRIP_ASPECT = re.compile(
    r"^(?:the\s+)?(?:order in which|sequence of|different aspects of|aspects of|timeline of|history of|"
    r"all the times i|details about)\s+", re.I,
)
_CORE_STRIP_MENTION = re.compile(
    r"^(?:i brought up|we talked about|we discussed|i mentioned)\s+", re.I)
_CORE_STRIP_ASPECT2 = re.compile(r"^(?:different aspects of|aspects of)\s+", re.I)
_CLAUSE_SPLIT = re.compile(r"\s*(?:;|,\s*and\b|\band then\b|\balso\b)\s*")
_WORD3 = re.compile(r"[A-Za-z]{3,}")
_COORD = re.compile(
    r"\b([a-z0-9_-]+(?:\s+[a-z0-9_-]+)?)\s+or\s+([a-z0-9_-]+(?:\s+[a-z0-9_-]+)?)\b",
    re.I,
)


def _core_topic(question: str) -> str:
    cleaned = _QUERY_FRAME.sub("", question.strip())
    cleaned = _QUERY_TRAILING.sub("", cleaned).strip(" ?,.:;")
    cleaned = _CORE_STRIP_ARTICLE.sub("", cleaned)
    cleaned = _CORE_STRIP_ASPECT.sub("", cleaned)
    cleaned = _CORE_STRIP_MENTION.sub("", cleaned)
    cleaned = _CORE_STRIP_ASPECT2.sub("", cleaned)
    return cleaned.strip(" ?,.:;")


_GAP_FRAME = re.compile(r"^(?:how (?:many|much|long)\b.*?\b(?:between|from)|"
                        r"what (?:is|was) the (?:gap|time|difference) between)\s+", re.I)
_GAP_SPLIT = re.compile(r"\s+(?:and|to|until)\s+(?=(?:when|the|my|i|a|an|what|where|how|our)\b)", re.I)
_GAP_ORDER = re.compile(r"^how (?:many|much|long)\b[\w\s]*?\b(after|before|since)\s+(.+?)\s+"
                        r"(?:did|do|was|were|had|have|could|would|when)\s+(?:i|we)\s+(.+)$", re.I)
_GAP_SINCE = re.compile(r"^how (?:many|much|long)\b.*?\b(?:had|have) (?:i|we) been\s+(.+?)\s+"
                        r"(?:when|before|by the time)\s+(?:i|we)\s+(.+)$", re.I)
_EVENT_LEAD = re.compile(r"^(?:when|the (?:day|time|week|moment) (?:when )?|the date )\s*", re.I)


def gap_events(question: str) -> list[str]:
    """The two events a date-gap question measures between, each searched on its own:
    "how many days passed between when I got my API key and when I finished the
    wireframe" also searches "I got my API key" and "I finished the wireframe"."""
    text = question.strip().rstrip("?.! ")
    frame = _GAP_FRAME.match(text)
    if frame:
        parts = _GAP_SPLIT.split(text[frame.end():], maxsplit=1)
        if len(parts) == 2:
            return [e for e in (_EVENT_LEAD.sub("", p).strip(" ,") for p in parts) if len(_WORD3.findall(e)) >= 2]
    order = _GAP_ORDER.match(text)
    if order:
        return [e for e in (order.group(2).strip(" ,"), order.group(3).strip(" ,")) if len(_WORD3.findall(e)) >= 2]
    since = _GAP_SINCE.match(text)
    if since:
        return [e for e in (since.group(1).strip(" ,"), since.group(2).strip(" ,")) if len(_WORD3.findall(e)) >= 2]
    return []


def subqueries(question: str) -> list[str]:
    """The question, plus each clause of a compound one, plus each aspect of a list
    ("a summary of X, including A, B and C" also searches "X A", "X B", "X C")."""
    out = [question]
    core = _core_topic(question)
    q_norm = question.rstrip(" ?,.:;").lower()
    if core and core.lower() != q_norm and len(core) >= 4:
        out.append(core)
    out += gap_events(question)
    parts = _CLAUSE_SPLIT.split(question)
    if len(parts) > 1:
        out += [p for p in parts if len(_WORD3.findall(p)) >= 2]
    coord = _COORD.search(core or question)
    if coord:
        w1, w2 = coord.group(1), coord.group(2)
        if w1.lower() not in profile.STOPWORDS and w2.lower() not in profile.STOPWORDS:
            out.append(question.replace(coord.group(0), w1))
            out.append(question.replace(coord.group(0), w2))
            if core:
                out.append(core.replace(coord.group(0), w1))
                out.append(core.replace(coord.group(0), w2))
    if question.count(",") >= 2 or re.search(r"\bincluding\b|:", question):
        head = re.split(r"\bincluding\b|:", question, maxsplit=1)[0]
        rest = question[len(head):]
        multilingual = unicode_index.relevant(question)
        head_words = [w for w in WORD_RE.findall(head) if len(w) >= 3 or has_cjk(w)] if multilingual \
            else re.findall(r"[A-Za-z][A-Za-z'-]{2,}", head)
        topic = [w for w in head_words
                 if w.lower() not in profile.STOPWORDS and w.lower() not in _FRAME_WORDS][:4]
        for aspect in re.split(r",\s*(?:and\s+)?|\band\b|\bincluding\b|:", rest):
            aspect_words = [w for w in WORD_RE.findall(aspect) if len(w) >= 3 or has_cjk(w)] if multilingual \
                else re.findall(r"[A-Za-z][A-Za-z'-]{2,}", aspect)
            words = [w for w in aspect_words if w.lower() not in profile.STOPWORDS]
            if words:
                if len(words) >= 2:
                    out.append(" ".join(words))
                if topic:
                    out.append(" ".join(topic + words))
    lead = _CONSIDERING.match(question)
    if lead:  # "Considering A, B and C, how ...": each named aspect is searched on its own
        for aspect in re.split(r",\s*(?:and\s+)?|\s+and\s+", lead.group(1)):
            words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]+", aspect)
                     if w.lower() not in profile.STOPWORDS and w.lower() not in _FRAME_WORDS]
            if len(words) >= 2:
                out.append(" ".join(words))
    return list(dict.fromkeys(out))[:12]


_CONSIDERING = re.compile(r"^(?:considering|given|taking into account|based on)\s+(.+?),\s*(?:how|what|which|can|could|"
                          r"should|would|will|do|does|is|are)\b", re.I)


SECOND_PASS_TURNS = 8  # most turns a coverage-driven second pass may add
SECOND_PASS_QUERIES = 8


def facet_queries(question: str, missing: Sequence[str]) -> list[str]:
    """Facet sub-queries for subject terms the first pass did not deliver.

    Each missing term is searched alone and beside each entity the question
    names ("Ana" + "dog"), then all of them together through `subqueries`.
    Deterministic: the same question and terms give the same queries.
    """
    terms = [t for t in dict.fromkeys(missing) if t]
    if not terms:
        return []
    named = list(dict.fromkeys(profile.entities(question)))[:3]
    out: list[str] = []
    for term in terms:
        out.append(term)
        out.extend(f"{name} {term}" for name in named)
    out.extend(subqueries(" ".join(terms)))
    return list(dict.fromkeys(out))[:SECOND_PASS_QUERIES]


def _rrf(rankings: list[tuple[list[int], float]]) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking, weight in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + weight / (RRF_K + rank + 1)
    return scores


def relative_time_delta(moment: dt.datetime, now: dt.datetime | None = None) -> str:
    """Return a human-readable relative time string (Letta-style).

    Examples: ``"12s ago"``, ``"4m ago"``, ``"2h ago"``, ``"5d ago"``.
    """
    ref = now or dt.datetime.utcnow()
    delta = ref - moment
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "in the future"
    if total_seconds < 60:
        return f"{total_seconds}s ago"
    if total_seconds < 3600:
        return f"{total_seconds // 60}m ago"
    if total_seconds < 86400:
        return f"{total_seconds // 3600}h ago"
    return f"{total_seconds // 86400}d ago"


def _normalize_text(text: str) -> str:
    """Lowercased, punctuation-stripped, whitespace-collapsed form for equality checks."""
    if text and unicode_index.relevant(text):
        normalized = unicodedata.normalize("NFC", text).lower()
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]|_", "", normalized)).strip()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", (text or "").lower())).strip()


def filter_self_turns(store: Store, question: str, turn_ids: list[int]) -> tuple[list[int], int]:
    """Drop turns that merely restate the question (anti-recursion filter).

    A just-asked user question is stored as a turn; recalling it as its own
    answer creates a self-loop where the context echoes the query instead of
    answering it. Returns (kept ids, n_dropped).
    """
    norm_q = _normalize_text(question)
    if not norm_q:
        return list(turn_ids), 0
    turns = store.turns(turn_ids)
    kept = [tid for tid in turn_ids
            if tid not in turns or _normalize_text(turns[tid].text) != norm_q]
    return kept, len(turn_ids) - len(kept)


def _embedder(store: Store, choice: str | None):
    from commontrace.conversation import embed

    tag = embed.configured() if choice == "auto" else choice
    if not tag or tag == "none":
        return None
    if not (embed.available() if tag in embed.MODELS else embed.available_for(tag)):
        return None
    with store._lock:
        if tag not in store._embedders:
            store._embedders[tag] = embed.Embedder(store.root, tag, read_only=store.read_only)
        return store._embedders[tag]


def _header(session: str, at: dt.datetime | None, now: dt.datetime | None = None) -> str:
    if at is None:
        return f"[{session}]"
    when = f"{timeparse.WEEKDAYS[at.weekday()].capitalize()} {timeparse.label(at.date())}"
    if at.hour or at.minute:
        when += at.strftime(", %H:%M")
    if now is not None:
        when += f" ({relative_time_delta(at, now)})"
    return f"[{session} · {when}]"


def _summary_line(text: str) -> str:
    return f"(session summary: {text})"


def _line(turn: Turn) -> str:
    return f"{turn.speaker}: {turn.annotated()}"


_WINDOW = re.compile(r"(?<=[.!?\n])\s+")
_UNICODE_WINDOW = re.compile(r"(?<=[。！？\n])\s*|(?<=[.!?])\s+")


def _unicode_words(text: str) -> set[str]:
    """Bounded-window terms shared with Unicode sparse evidence matching."""
    normalized = unicodedata.normalize("NFC", text).lower()
    return {term for match in WORD_RE.finditer(normalized) for term in segment_cjk(match.group())}


def _unicode_excerpt(turn: Turn, question: str, cap: int) -> str:
    """Select an exact source span, retaining sentence boundaries when possible.

    Overlapping windows keep relevant tails searchable even without spaces or
    punctuation. Ellipses mark every omitted edge; attribution and those marks
    count toward the same character-based token budget as the quoted body.
    """
    limit = max(0, cap) * 4
    prefix = turn.speaker + ": "
    text = turn.annotated()
    # Reserve both omission marks before scoring, rather than clipping a
    # selected passage afterwards and accidentally dropping its matching tail.
    width = limit - len(prefix) - 4
    if width <= 0:
        return prefix[:limit]
    asked = {w for w in _unicode_words(question)
             if (len(w) > 2 or has_cjk(w)) and w not in profile.STOPWORDS}
    singletons = {w for w in asked if len(w) == 1 and has_cjk(w)}
    best: tuple[int, bool, int] | None = None
    selected = (0, min(width, len(text)))
    start = 0
    boundaries = (match.end() for match in _UNICODE_WINDOW.finditer(text))
    for end in chain(boundaries, (len(text),)):
        # Only a single bounded span is materialized per comparison. Long turns
        # do not create a corpus-sized token set or a list of overlapping text.
        span_start, span_end = start, end
        while span_start < span_end and text[span_start].isspace():
            span_start += 1
        while span_end > span_start and text[span_end - 1].isspace():
            span_end -= 1
        complete = span_end - span_start <= width
        offset = span_start
        while offset < span_end:
            stop = min(span_end, offset + width)
            passage = text[offset:stop]
            matching = asked & _unicode_words(passage)
            matching.update(word for word in singletons if word in passage)
            score = (len(matching), complete, -offset)
            if best is None or score > best:
                best, selected = score, (offset, stop)
            if stop == span_end:
                break
            offset = min(offset + max(1, width // 2), span_end - width)
        start = end
    lo, hi = selected
    return prefix + ("… " if lo else "") + text[lo:hi] + (" …" if hi < len(text) else "")


def _excerpt(turn: Turn, question: str, cap: int) -> str:
    """A long turn cut to the passages that bear on the question (in their original
    order, gaps marked), so one pasted log or long answer cannot crowd out the rest."""
    full = _line(turn)
    if tokens(full) <= cap:
        return full
    if unicode_index.relevant(question) or unicode_index.relevant(full):
        return _unicode_excerpt(turn, question, cap)
    asked = {w for w in re.findall(r"[a-z0-9_]+", question.lower()) if len(w) > 2 and w not in profile.STOPWORDS}
    pieces, buf = [], ""
    for part in _WINDOW.split(turn.annotated()):
        buf = f"{buf} {part}".strip()
        if len(buf) >= 280:
            pieces.append(buf)
            buf = ""
    if buf:
        pieces.append(buf)
    scored = sorted(range(len(pieces)), key=lambda i: (
        -len(asked & set(re.findall(r"[a-z0-9_]+", pieces[i].lower()))), i))
    keep: set[int] = set()
    spent = tokens(turn.speaker) + 4
    for i in scored:
        cost = tokens(pieces[i]) + 1
        if spent + cost > cap and keep:
            continue
        keep.add(i)
        spent += cost
        if spent >= cap:
            break
    out, previous = [], -1
    for i in sorted(keep):
        if i != previous + 1:
            out.append("…")
        out.append(pieces[i][: cap * 4])
        previous = i
    if previous != len(pieces) - 1:
        out.append("…")
    return f"{turn.speaker}: " + " ".join(out)


_RECALL_CACHE_MAX = 100
_RECALL_CACHE_BYTES = 8 * 1024 * 1024
_RECALL_CACHE: OrderedDict = OrderedDict()
_RECALL_LOCK = threading.Lock()


def _recall_size(result: Recall) -> int:
    # Conservative bound including the copied metadata and candidate lists.
    return len(result.context.encode("utf-8")) + len(result.question.encode("utf-8")) \
        + 128 * (len(result.ranked) + len(result.turns)) + 4096


def forget_store(store: Store) -> None:
    with _RECALL_LOCK:
        for key in list(_RECALL_CACHE):
            if key[0] is store.cache_identity:
                del _RECALL_CACHE[key]


def _recall_key(store: Store, question: str, now, opts: Options,
                extra_queries: Sequence[str]) -> tuple | None:
    """Cache key for a recall, or None when the call must not be cached.

    Includes local change count and SQLite data_version: deletions, profile
    changes and commits through other connections invalidate the context.
    Calls with an explicit `now` are keyed on it too, since headers render
    relative deltas against that moment.
    """
    try:
        stamp = store.read_stamp()
        # With an implicit clock an expiry can pass without a write. Do not
        # cache that view; explicit moments are stable and safe to cache.
        if now is None and store._has_expiry and store.db.execute(
                "SELECT 1 FROM turns WHERE expires IS NOT NULL LIMIT 1").fetchone():
            return None
    except Exception:  # noqa: BLE001 - no cache without a readable store
        return None
    moment = timeparse.parse_moment(now) if isinstance(now, str) else now
    return (
        store.cache_identity, store.path, question, str(moment or ""),
        opts.budget, opts.pool, opts.neighbours_before, opts.neighbours_after,
        opts.neighbour_hits, opts.neighbour_minutes, opts.bridge_turns, opts.excerpt_tokens,
        opts.window_boost, opts.entity_boost, opts.lexical_weight, opts.rerank, opts.recency_pool,
        opts.rerank_depth, opts.rerank_blend, opts.profile_facts, opts.instructions,
        opts.broad, opts.recency_boost, opts.primary_hits, opts.embedder,
        opts.summaries, opts.sessions, opts.speakers, opts.since, opts.until, opts.graph_hops,
        opts.context_strategy, opts.second_pass, tuple(extra_queries), stamp,
    )


AUTO_BUDGET_FACTORS = {"summary": 3.0, "ordering": 3.0, "counting": 3.0, "broad": 2.0,
                       "multi-facet": 2.0, "list": 2.0, "focused": 1.0}
_LIST_QUESTION = re.compile(
    r"^\s*(?:what|which)\s+(?:(?:kinds?|types?|sorts?)\s+of\s+[a-z][a-z-]*"
    r"|(?!(?:is|was|does|has|this|these|those|its)\b)[a-z][a-z-]{2,}s)\s+(?:do|does|did|has|have|had|are|were)\b"
    r"|\b(?:both|in common|all of (?:the|my|his|her|their))\b"
    r"|^\s*(?:what|which|where|who)\b[^?]*\b(?:has|have)\s+\w+\s+(?:\w+\s+)?(?:done|made|seen|visited|read|"
    r"bought|tried|painted|attended|played|taken|used|owned|written|watched|met)\b", re.I)


def budget_for(question: str, base: int, cap: int = 12_000) -> tuple[int, str]:
    """A context budget sized to the question's shape, never below `base`.

    A focused question ("where does Ana work?") keeps `base`. One that needs
    coverage rather than the single best passage -- a summary, an order of events,
    a count, a list across sessions, several facets at once -- gets a multiple of
    it, capped at `cap`. Deterministic: the same question always gets the same size.
    """
    if not isinstance(base, int) or base <= 0:
        raise ConversationError("budget must be a positive integer")
    if not isinstance(cap, int) or cap < base:
        raise ConversationError("max_budget must be an integer no smaller than budget")
    q = question or ""
    if _SUMMARY.search(q):
        reason = "summary"
    elif _ORDERING.search(q):
        reason = "ordering"
    elif _COUNTING.search(q) or gap_events(q):
        reason = "counting"
    elif is_broad(q):
        reason = "broad"
    elif len(subqueries(q)) > 2 or _RELATIONAL.search(q):
        reason = "multi-facet"
    elif _LIST_QUESTION.search(q):
        reason = "list"
    else:
        reason = "focused"
    return min(cap, int(base * AUTO_BUDGET_FACTORS[reason])), reason


def recall(store: Store, question: str, *, now=None, options: Options | None = None,
           extra_queries: Sequence[str] = (), dense_candidates: DenseCandidates | None = None) -> Recall:
    """`extra_queries` are searched beside the question, each keeping its own best
    ranks (a follow-up search that finds a missing fact first is not diluted).

    Repeat questions use a bounded LRU invalidated by local and external writes.
    Results are independent copies; closing the store releases its cached data.

    With ``Options(adaptive_budget=True)`` the budget is resolved first by
    `budget_for`; ``explain["budget"]`` records what was asked, used and why.
    """
    if options is not None and options.adaptive_budget:
        effective, reason = budget_for(question, options.budget, options.max_budget)
        resolved = dataclasses.replace(options, budget=effective, adaptive_budget=False)
        result = recall(store, question, now=now, options=resolved, extra_queries=extra_queries,
                        dense_candidates=dense_candidates)
        result.explain["budget"] = {"requested": options.budget, "effective": effective, "reason": reason}
        return result
    from commontrace import telemetry

    if question and unicode_index.relevant(question):
        _context_strategy((options or Options()).context_strategy)
        store.prepare_lexical(question)
    with store.read_snapshot(), telemetry.span(
            "conversation.recall", space=store.space, queries=1 + len(extra_queries)) as handle:
        opts = options or Options()
        _context_strategy(opts.context_strategy)
        key = _recall_key(store, question, now, opts, extra_queries) if dense_candidates is None else None
        if key is not None:
            with _RECALL_LOCK:
                hit = _RECALL_CACHE.get(key)
                if hit is not None:
                    _RECALL_CACHE.move_to_end(key)
                    handle.set(tokens=hit.tokens, turns=len(hit.turns), cached=True)
                    return copy.deepcopy(hit)
        result = _recall(store, question, now=now, options=options, extra_queries=extra_queries,
                         dense_candidates=dense_candidates)
        handle.set(tokens=result.tokens, turns=len(result.turns))
        if key is not None and _recall_size(result) <= _RECALL_CACHE_BYTES:
            with _RECALL_LOCK:
                # Remove older revisions of this store before retaining another
                # copy of potentially sensitive evidence.
                for old in list(_RECALL_CACHE):
                    if old[0] is store.cache_identity and old[-1] != key[-1]:
                        del _RECALL_CACHE[old]
                _RECALL_CACHE[key] = copy.deepcopy(result)
                _RECALL_CACHE.move_to_end(key)
                while len(_RECALL_CACHE) > _RECALL_CACHE_MAX or sum(
                        _recall_size(r) for r in _RECALL_CACHE.values()) > _RECALL_CACHE_BYTES:
                    _RECALL_CACHE.popitem(last=False)
        return result


def _recall(store: Store, question: str, *, now=None, options: Options | None = None,
            extra_queries: Sequence[str] = (), dense_candidates: DenseCandidates | None = None) -> Recall:
    opts = options or Options()
    _context_strategy(opts.context_strategy)
    question = (question or "").strip()
    if not question:
        return Recall(question, "", 0, [], [], None)
    moment = timeparse.parse_moment(now) if isinstance(now, str) else now
    if now is not None and moment is None:
        raise ConversationError(f"unrecognised date {now!r}")
    if isinstance(moment, dt.datetime):
        moment = moment.replace(tzinfo=None)
    moment = moment or store.latest_moment()
    window = timeparse.question_window(question, moment)
    if dense_candidates is not None:
        from commontrace.conversation import embed

        tag = embed.configured() if opts.embedder == "auto" else opts.embedder
        expected_model = embed.model_identity(tag) if tag and tag != "none" else None
        if dense_candidates.identity != (store._units_identity or store.cache_identity) \
                or dense_candidates.stamp != store.unit_stamp() or dense_candidates.model != expected_model:
            raise ConversationError("external vector candidates are stale or use a different embedding model")
    embedder = _embedder(store, opts.embedder) if dense_candidates is None else None
    until: str | dt.datetime | None = opts.until
    if now is not None and moment is not None:
        parsed_until = timeparse.parse_moment(opts.until) if opts.until else moment
        if parsed_until is None:
            raise ConversationError(f"unrecognised date {until!r}")
        until = min(parsed_until, moment)
    allowed = store.allowed(sessions=opts.sessions, speakers=opts.speakers, since=opts.since,
                            until=until, now=moment if now is not None else None)
    if allowed is not None and len(allowed) == store.turn_count():
        # a filter that excludes nothing is no filter: every search arm would otherwise
        # carry the whole id list into each full-text query
        allowed = None
    pool = opts.pool
    queries = list(dict.fromkeys(subqueries(question) + [q.strip() for q in extra_queries if q and q.strip()]))
    dense_rankings: dict[str, list[int]] = {
        query: list(units) for query, units in dense_candidates.rankings.items()
    } if dense_candidates is not None else {}
    def encode_dense(batch_queries: list[str]) -> None:
        if embedder is None or allowed == set() or pool <= 0 or not batch_queries:
            return
        from commontrace.conversation import embed

        # Encode related facets together and scan each bounded query batch once.
        for start in range(0, len(batch_queries), embed.QUERY_BATCH):
            batch = batch_queries[start:start + embed.QUERY_BATCH]
            vectors = embedder.encode(batch, query=True)
            pages = embed.search_many(store, embedder, vectors, pool, allowed=allowed)
            dense_rankings.update((q, [u for u, _s in hits]) for q, hits in zip(batch, pages))

    encode_dense(queries)

    def arm_rankings(query: str) -> list[tuple[list[int], float]]:
        lexical = [u for u, _s in store.lexical(query, pool, allowed=allowed)]
        if embedder is None and dense_candidates is None:
            return [(lexical, 1.0)]
        return [(dense_rankings.get(query, []), 1.0), (lexical, opts.lexical_weight)]

    def turn_scores(queries: list[str]) -> dict[int, float]:
        by_turn: dict[int, float] = {}
        for query in queries:
            ranked_turns = []
            for units, weight in arm_rankings(query):
                unit_turn = store.unit_turns(units)
                ranked_turns.append((list(dict.fromkeys(
                    unit_turn[u] for u in units if u in unit_turn and (allowed is None or unit_turn[u] in allowed))),
                    weight))
            for turn, score in _rrf(ranked_turns).items():
                by_turn[turn] = max(by_turn.get(turn, 0.0), score)
        return by_turn

    scores = turn_scores(queries)
    explain: dict = {"subqueries": queries}
    if allowed is not None:
        explain["filtered_to"] = len(allowed)
    if opts.entity_boost and scores:
        named = set(profile.entities(question))
        try:
            from commontrace import entity_store as _entity_store

            _root = getattr(store, "root", "")
            if _root:
                named |= _entity_store.confirmed_query_entities(_root, question)
        except Exception:
            pass
        if named:
            top = max(scores.values())
            boosted = 0
            for name, turns in store.entity_turns(named, max_matches=pool).items():
                weight = 1.0 / (1.0 + 0.001 * (len(turns) - 1) ** 2) if turns else 0.0
                for turn in turns:
                    if allowed is None or turn in allowed:
                        scores[turn] = scores.get(turn, 0.0) + opts.entity_boost * top * weight
                        boosted += 1
            explain["entities"] = sorted(named)
    if window is not None:
        inside = store.in_window(window[0], window[1])
        inside = inside if allowed is None else inside & allowed
        explain["window_turns"] = len(inside)
        top = max(scores.values(), default=0.0)
        for turn in inside:
            if turn in scores:
                scores[turn] += opts.window_boost * top * 0.5
    if opts.recency_boost and scores and asks_current(question):
        # knowledge updates: among comparable matches the later statement wins
        # Only near-equal matches compete on time: a boost spread over every
        # scored turn lets recent chatter outrank the one turn that answers.
        top = max(scores.values())
        comparable = sorted(scores, key=lambda t: (-scores[t], t))[:max(1, opts.recency_pool)]
        candidates = store.turns(comparable)
        order = sorted(comparable, key=lambda t: (candidates[t].at if t in candidates and candidates[t].at
                                                  else dt.datetime.min, t))
        for position, turn in enumerate(order):
            scores[turn] += opts.recency_boost * top * (position / max(1, len(order) - 1))
        explain["recency"] = True
    asks_when = bool(_ASKS_WHEN.search(question or ""))
    if asks_when and scores:
        top = max(scores.values())
        top_candidates = list(scores.keys())[:100]
        dated_turns = set()
        for r in store.db.execute(
            f"SELECT id FROM turns WHERE id IN ({','.join('?' for _ in top_candidates)}) AND dates != '[]'",  # nosec B608 - ?-placeholders only
            top_candidates,
        ):
            dated_turns.add(r[0])
        for turn in dated_turns:
            # proportional: a dated turn rises among its peers instead of jumping irrelevant ones
            scores[turn] += 0.4 * scores[turn]
        explain["asks_when"] = True
    ranked = sorted(scores, key=lambda t: (-scores[t], t))
    ranked, n_self = filter_self_turns(store, question, ranked)
    if n_self:
        explain["self_filtered"] = n_self
    hops = opts.graph_hops if opts.graph_hops is not None else min(2, len(_RELATIONAL.findall(question)))
    if hops and ranked:
        graph, paths = _graph_candidates(store, question, ranked, allowed, hops)
        graph, _ = filter_self_turns(store, question, graph)
        # Reserve a small discovery quota so graph-only evidence is not drowned
        # by hundreds of near-identical lexical/dense matches.
        head = set(ranked[:6])
        novel = [t for t in graph if t not in head]
        # Every discovered turn remains a candidate, including later two-hop
        # evidence. Previously only the first four discoveries reached recall.
        seen = set(ranked)
        for turn in novel:
            if turn not in seen:
                seen.add(turn)
                ranked.append(turn)
        for i, turn in enumerate(novel):
            if i >= 4:
                break
            ranked = [t for t in ranked if t != turn]
            ranked.insert(min(2 + 2 * i, len(ranked)), turn)
        explain["graph_paths"] = paths
    rerank = opts.rerank
    if rerank == "auto":
        rerank = "cross-encoder" if embedder is not None else None
    if rerank and ranked:
        ranked = _rerank(store, question, ranked, rerank, opts.rerank_depth, explain, opts.rerank_blend)
        explain["rerank"] = rerank
    withheld: list[int] = []
    # Relative deltas are shown only when the caller supplies a reference
    # moment: defaulting to the latest turn would print "0s ago" noise.
    show_now = moment if now is not None else None
    belief_at = moment if now is not None else None
    if belief_at is None and window is not None and moment is not None and window[1] < moment.date():
        belief_at = dt.datetime.combine(window[1], dt.time(23, 59, 59))
        explain["belief_as_of"] = belief_at.isoformat()
    from commontrace.conversation.coverage import assess

    def pack(candidates: list[int], withheld: list[int]):
        emitted: list[_EmittedText] = []
        selection: dict = {}
        packed = assemble(store, question, candidates, opts, withheld, allowed, now=show_now,
                          as_of=belief_at, current_instructions=now is None and belief_at is not None,
                          evidence_paths=explain.get("graph_paths", ()), emitted=emitted, selection=selection)
        found = assess(question, [part.body for part in emitted if part.body],
                       labels=[part.speaker for part in emitted if part.body and part.speaker])
        return packed, selection, found

    (context, used, n_tokens), selection, coverage = pack(ranked, withheld)
    if opts.second_pass:
        ranked, withheld, (context, used, n_tokens), selection, coverage = _second_pass(
            store, question, ranked, withheld, (context, used, n_tokens), selection, coverage, opts, explain,
            encode_dense, turn_scores, pack)
    explain["context_selection"] = selection
    if explain.get("graph_paths"):
        chosen = set(used)
        explain["selected_graph_paths"] = [p for p in explain["graph_paths"]
                                           if p["source"] in chosen and p["turn"] in chosen]
    conf = coverage.confidence
    explain["confidence"] = conf
    explain["coverage"] = coverage.as_dict()
    if conf == 0.0:
        explain["abstain"] = True
    if withheld:
        explain["withheld"] = withheld
    return Recall(question, context, n_tokens, used, ranked,
                  (window[0].isoformat(), window[1].isoformat(), window[2]) if window else None, explain)


def _second_pass(store: Store, question: str, ranked: list[int], withheld: list[int], packed: tuple,
                 selection: dict, coverage, opts: Options, explain: dict, encode_dense, turn_scores, pack):
    """Search facet queries for the subject terms the first context missed.

    Runs only when the first pass did not abstain, some subject term is
    missing and the context left budget unused. Newly found turns go after
    the first-pass ranking, so the greedy assembly keeps what it had and fills
    the room left; the same budget applies. The result is used only when it
    delivers at least one new turn. ``explain["second_pass"]`` reports it.
    """
    missing = list(coverage.missing_subject_terms)
    report: dict = {"terms": missing, "added_turns": []}
    unchanged = ranked, withheld, packed, selection, coverage
    if coverage.abstain:
        report["skipped"] = "the first pass abstained"
    elif not missing:
        report["skipped"] = "no subject term is missing"
    elif packed[2] >= opts.budget:
        report["skipped"] = "no budget remains"
    if "skipped" in report:
        explain["second_pass"] = report
        return unchanged
    queries = facet_queries(question, missing)
    encode_dense(queries)
    scores = turn_scores(queries)
    found, _self = filter_self_turns(store, question, sorted(scores, key=lambda t: (-scores[t], t)))
    known = set(ranked)
    added = [t for t in found if t not in known][:SECOND_PASS_TURNS]
    report["queries"] = queries
    if not added:
        report["skipped"] = "the facet queries found no new turn"
        explain["second_pass"] = report
        return unchanged
    merged = ranked + added
    second_withheld: list[int] = []
    packed2, selection2, coverage2 = pack(merged, second_withheld)
    before = set(packed[1])
    new = [t for t in packed2[1] if t not in before]
    report.update(added_turns=new, tokens_before=packed[2], tokens_after=packed2[2],
                  missing_after=list(coverage2.missing_subject_terms))
    explain["second_pass"] = report
    if not new:
        report["skipped"] = "the new turns did not fit the remaining budget"
        return unchanged
    return merged, second_withheld, packed2, selection2, coverage2


def _rerank(store: Store, question: str, ranked: list[int], mode: str, depth: int,
            explain: dict | None = None, blend: float = 0.0) -> list[int]:
    from commontrace import rerank_arm

    if mode not in rerank_arm.MODELS:
        return _rerank_provider(store, question, ranked, mode, depth, explain, blend)
    if rerank_arm.ready(mode):
        return ranked
    head = ranked[:depth]
    turns = store.turns(head)
    # the cross-encoder reads ~512 tokens: give it the passages of a long turn that bear on
    # the question rather than its opening, which is also several times faster
    text_of = {str(t): _excerpt(turns[t], question, 300) for t in head if t in turns}
    page, _ = rerank_arm.rerank(question, list(text_of), text_of, len(text_of), mode=mode)
    order = [int(s) for s, _x in page]
    if explain is not None and page:
        explain["rerank_top"] = round(float(page[0][1]), 4)
    if blend:
        # the cross-encoder votes beside the fused ranking instead of replacing it: it was
        # trained on question -> passage search and misjudges task requests and diary notes
        fused = {t: i for i, t in enumerate(head)}
        scores = {t: 1.0 / (RRF_K + fused.get(t, len(head)) + 1) + blend / (RRF_K + i + 1)
                  for i, t in enumerate(order)}
        order = sorted(scores, key=lambda t: -scores[t])
    returned = set(order)
    return order + [t for t in ranked if t not in returned]


def _rerank_provider(store: Store, question: str, ranked: list[int], name: str, depth: int,
                     explain: dict | None, blend: float) -> list[int]:
    """Any `providers.reranker` text reranker (hosted, LLM or registered) over the head.

    ``blend`` keeps this module's meaning: the reranker's weight beside the
    fused rank, where 0 lets it replace that rank. A reranker that cannot run
    leaves the ranking unchanged and is reported in ``explain["rerank_stage"]``.
    """
    from commontrace import reranking

    report: dict
    try:
        ranker, unavailable = reranking.load(name)
    except ValueError as exc:
        ranker, unavailable = None, str(exc)
    if ranker is None:
        report = {"reranker": name, "items": 0, "latency_ms": 0.0, "error": unavailable}
        order = ranked
    else:
        head = ranked[:max(1, min(depth, reranking.MAX_DEPTH))]
        turns = store.turns(head)
        text_of = {str(t): _excerpt(turns[t], question, 300) for t in head if t in turns}
        stage_blend = 1.0 if not blend else blend / (1.0 + blend)
        keys, report = reranking.stage(question, [str(t) for t in ranked], text_of, ranker,
                                       depth=len(head), blend=stage_blend)
        order = [int(k) for k in keys]
        if report.get("top"):
            report["top"] = [{"id": int(row["id"]), "score": row["score"]} for row in report["top"]]
    if explain is not None:
        explain["rerank_stage"] = report
        if report.get("top"):
            explain["rerank_top"] = report["top"][0]["score"]
    return order


_GLOBAL_RULE = re.compile(r"\b(?:format\w*|style|length|short|shorter|concise|brief|bullet\w*|list|language|units?|"
                          r"metric|imperial|code|snippets?|syntax|tone|formal|casual|words?|examples?|step|steps|"
                          r"explain\w*|answers?|responses?|repl(?:y|ies)|summar\w*|cite|sources?|emoji\w*)\b", re.I)


def _instruction_lines(store: Store, limit: int, question: str = "", *,
                       facts: list[dict] | None = None,
                       emissions: dict[tuple[str, int], _EmittedText] | None = None) -> list[tuple[str, int]]:
    """Standing instructions the user gave the assistant: they apply to every answer, so
    they are shown whatever the question, the ones touching its subject and the rules
    about how to answer (format, length, tone, units, code) first."""
    if limit <= 0:
        return []
    # instructions are addressed to an assistant: a chat between two people has none
    addressed = store.db.execute("SELECT 1 FROM turns WHERE role='assistant' LIMIT 1").fetchone() is not None or \
        store.db.execute("SELECT COUNT(DISTINCT speaker) FROM turns").fetchone()[0] <= 1
    if not addressed:
        return []
    asked = _stems(question)
    picked, seen = [], set()
    for f in reversed(facts if facts is not None else store.facts(["instruction"])):
        if f["kind"] != "instruction":
            continue
        key = (f["owner"], f["subject"])
        if key in seen:
            continue
        seen.add(key)
        overlap = len(asked & _stems(f["statement"])) + (1 if _GLOBAL_RULE.search(f["statement"]) else 0)
        picked.append((overlap, f))
    picked.sort(key=lambda x: -x[0])  # most relevant first; newest first among equals
    out = []
    for _overlap, f in picked[:limit]:
        day = f"({f['at'][:10]}) " if f["at"] else ""
        item = (f"- {day}{f['statement']}", f["turn"])
        out.append(item)
        if emissions is not None:
            emissions[item] = _EmittedText(f["statement"])
    return out


def _profile_lines(store: Store, question: str, limit: int, *,
                   facts: list[dict] | None = None,
                   emissions: dict[tuple[str, int], _EmittedText] | None = None) -> list[tuple[str, int]]:
    if limit <= 0:
        return []
    facts = [f for f in (facts if facts is not None else store.facts()) if f["kind"] != "instruction"]
    if not facts:
        return []
    asked = _stems(question)
    advice = bool(_ADVICE.search(question))
    about_self = bool(_ABOUT_SELF.search(question))
    preference = bool(_PREFERENCE.search(question))
    scored = []
    for f in facts:
        overlap = len(asked & _stems(f["statement"])) + (2 if about_self and f["kind"] == "identity" else 0)
        overlap += 2 if asked & _stems(f["owner"]) else 0
        is_pref_fact = f["kind"] in ("preference", "dislike", "favorite", "habit")
        matches_advice = advice and f["kind"] in ("preference", "dislike", "favorite", "identity")
        if overlap or matches_advice or (preference and is_pref_fact):
            pref_boost = 1.5 if (preference and is_pref_fact) else 0.0
            bias = 0.5 if f["kind"] in ("preference", "dislike") else 0.0
            score = overlap + pref_boost + bias
            scored.append((score, f))
    scored.sort(key=lambda x: x[1]["at"] or "", reverse=True)
    scored.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _score, f in scored:
        identity = (f["owner"], f["statement"])
        if identity in seen:
            continue
        seen.add(identity)
        day = f"({f['at'][:10]}) " if f["at"] else ""
        owner = f["speaker"] if f["speaker"].lower() == f["owner"] else f["owner"]
        item = (f"- {day}{owner}: {f['statement']}", f["turn"])
        out.append(item)
        if emissions is not None:
            emissions[item] = _EmittedText(f["statement"], owner)
        if len(out) >= limit:
            break
    return out


def _flagged(turn: Turn) -> bool:
    return bool(injection_guard.injection_labels({"text": turn.text}))


def _bridge(store, turn, chosen: dict, span: int) -> list[int]:
    """Turns between `turn` and the nearest chosen turns of its session within `span` turns."""
    window = store.neighbours(turn, span + 1, span + 1)
    around = store.turns(window)
    placed = [around[w].idx for w in window if w in chosen and w in around]
    below = max((i for i in placed if i < turn.idx), default=None)
    above = min((i for i in placed if i > turn.idx), default=None)
    return [w for w in window if w in around and w not in chosen and (
        (below is not None and below < around[w].idx < turn.idx)
        or (above is not None and turn.idx < around[w].idx < above))]


def assemble(store: Store, question: str, ranked: list[int], opts: Options,
             withheld: list[int] | None = None, allowed: set[int] | None = None,
             now: dt.datetime | None = None, as_of=None,
             current_instructions: bool = False, evidence_paths=(),
             emitted: list[_EmittedText] | None = None,
             selection: dict | None = None) -> tuple[str, list[int], int]:
    """Fill the budget best-first, each hit with its neighbours, then render by time.
    A turn the injection screen flags is never shown; its id goes to `withheld`."""
    strategy = _context_strategy(opts.context_strategy)
    if selection is not None:
        selection.clear()
        selection["strategy"] = strategy
    withheld = [] if withheld is None else withheld
    if emitted is not None:
        emitted.clear()
    budget = max(0, opts.budget)
    facts = store.recall_facts(question, as_of=as_of, allowed=allowed,
                              instructions=bool(opts.instructions), profile_facts=bool(opts.profile_facts)) \
        if opts.instructions or opts.profile_facts else []
    if current_instructions and opts.instructions:
        facts = [f for f in facts if f["kind"] != "instruction"] + [
            f for f in store.recall_facts(question, allowed=allowed, profile_facts=False)
            if f["kind"] == "instruction"]
    # The same eligibility and injection checks apply to every memory layer.
    sources = store.fact_source_ids(f["id"] for f in facts)
    fact_turns = store.turns(t for ids in sources.values() for t in ids)
    flagged = {tid for tid, turn in fact_turns.items() if _flagged(turn)}
    withheld.extend(sorted(flagged))
    facts = [f for f in facts if sources.get(f["id"]) and not flagged.intersection(sources[f["id"]])
             and not injection_guard.injection_labels({"text": f["statement"]})
             and _normalize_text(f["statement"]) != _normalize_text(question)]
    profile_emissions: dict[tuple[str, int], _EmittedText] = {}
    instruction_lines = _instruction_lines(store, opts.instructions, question, facts=facts,
                                           emissions=profile_emissions)
    instruction_block = ("[Standing instructions from the user]\n" + "\n".join(t for t, _ in instruction_lines)
                         + "\n\n") if instruction_lines else ""
    while instruction_lines and tokens(instruction_block) > budget // 6:
        instruction_lines.pop()
        instruction_block = ("[Standing instructions from the user]\n" + "\n".join(t for t, _ in instruction_lines)
                             + "\n\n") if instruction_lines else ""
    profile_lines = _profile_lines(store, question, opts.profile_facts, facts=facts,
                                   emissions=profile_emissions)
    profile_title = "What the user has said about themselves"
    if as_of is not None:
        profile_at = timeparse.parse_moment(as_of)
        if profile_at is None:
            raise ConversationError("unrecognised belief date")
        profile_title += f"; beliefs as of {profile_at.isoformat()}"
    profile_block = (f"[{profile_title}]\n" + "\n".join(t for t, _ in profile_lines)
                     + "\n\n") if profile_lines else ""
    if tokens(profile_block) > budget // 4:
        profile_block, profile_lines = "", []
    profile_block = instruction_block + profile_block
    pinned = [turn for _t, turn in instruction_lines + profile_lines]
    spent = tokens(profile_block)
    chosen: dict[int, Turn] = {}
    sessions_seen: set[str] = set()
    # A session summary may include filtered, expired or future turns. In a
    # restricted view use exact eligible evidence rather than a wider summary.
    summaries = {s: f for s, f in store.summaries().items()
                 if not injection_guard.injection_labels({"text": f["text"]})} \
        if opts.summaries and allowed is None and as_of is None else {}

    def header_cost(session: str, at) -> int:
        cost = tokens(_header(session, at, now)) + 1
        if session in summaries:
            cost += tokens(_summary_line(summaries[session]["text"])) + 1
        return cost

    broad = is_broad(question) if opts.broad is None else opts.broad
    cap = opts.excerpt_tokens or (max(60, min(120, budget // 30)) if broad else max(200, budget // 5))
    summary = broad and bool(_SUMMARY.search(question or ""))
    if summary and not opts.excerpt_tokens:
        cap = max(SUMMARY_EXCERPT, budget // 60)  # many short exchanges over a few long ones
    about_user_only = broad and bool(re.search(
        r"\b(?:i (?:brought up|raised|mentioned|asked|said|wanted)|my questions?|"
        r"(?:did|have|do) i (?:ever )?(?:mention|bring up|raise|ask about|talk about))\b", question or "", re.I
    ))
    # "In what order did I bring up X": the answer is the user's own turns, in order. Every
    # one is a candidate, after the ranked ones, each cut to its passage nearest the
    # question so the whole sequence fits; similarity alone misses the later aspects.
    raised: list[int] = []
    if about_user_only and (_ORDERING.search(question or "") or _COUNTING.search(question or "")) \
            and not opts.excerpt_tokens:
        raised = [r[0] for r in store.db.execute(
            "SELECT id FROM turns WHERE role IN ('user', '') ORDER BY at, id LIMIT ?", (ORDERING_TURNS,))
            if allowed is None or r[0] in allowed]
        if raised:
            cap = min(cap, max(ORDERING_EXCERPT, int(budget * 0.9) // len(raised)))
    rendered: dict[int, str] = {}
    # One injection screen per turn per recall: neighbours re-fetch turns the
    # primary pass already screened, so memoize by turn id (single-run scope,
    # no staleness -- turns are immutable).
    _flag_cache: dict[int, bool] = {}

    def _is_flagged(turn: Turn) -> bool:
        hit = _flag_cache.get(turn.id)
        if hit is None:
            hit = _flagged(turn)
            if len(_flag_cache) < 4096:
                _flag_cache[turn.id] = hit
        return hit

    def line_of(turn: Turn) -> str:
        if turn.id not in rendered:
            rendered[turn.id] = _excerpt(turn, question, cap)
        return rendered[turn.id]

    parents = {p["turn"]: p["source"] for p in evidence_paths}
    question_norm = _normalize_text(question)

    def evidence_ids(tid: int) -> list[int]:
        # A graph answer and its connecting passages are one evidence unit.
        # Keep at most the two traversed ancestors, with cycle protection.
        ids = [tid]
        for _ in range(2):
            parent = parents.get(ids[-1])
            if parent is None or parent in ids:
                break
            ids.append(parent)
        return ids

    def evidence_group(tid):
        ids = evidence_ids(tid)
        turns = store.turns(ids)
        for i in ids:
            t = turns.get(i)
            if t is None or (allowed is not None and i not in allowed) or _normalize_text(t.text) == question_norm:
                return {}
            if _is_flagged(t):
                if i not in withheld:
                    withheld.append(i)
                return {}
        return turns

    def evidence_cost(turns):
        cost, new_sessions = 0, set()
        for tid, turn in turns.items():
            if tid in chosen:
                continue
            cost += tokens(line_of(turn)) + 1
            if turn.session not in sessions_seen and turn.session not in new_sessions:
                cost += header_cost(turn.session, turn.at)
                new_sessions.add(turn.session)
        return cost

    stream = list(ranked)
    if broad and ranked:
        from collections import defaultdict
        session_to_tids: dict[str, list[int]] = defaultdict(list)
        all_head = store.turns(ranked[:200])
        for tid in ranked[:200]:
            t = all_head.get(tid)
            sess = t.session if t else str(tid)
            session_to_tids[sess].append(tid)
        for sess in session_to_tids:
            session_to_tids[sess].sort(
                key=lambda tid: (0 if all_head.get(tid) and all_head[tid].role in ("user", "") else 1)
            )
        diversified = []
        max_depth = max((len(tids) for tids in session_to_tids.values()), default=0)
        for d in range(max_depth):
            for sess, tids in session_to_tids.items():
                if d < len(tids):
                    diversified.append(tids[d])
        diversified_set = set(diversified)
        stream = diversified + [t for t in ranked if t not in diversified_set]
    if raised:
        listed = set(stream)
        stream += [t for t in raised if t not in listed]

    priority_quota = 0
    if strategy == "coverage-v1":
        from commontrace.conversation.context_selection import (
            MAX_CANDIDATES,
            MAX_PASSAGE_CHARS,
            MAX_TOKEN_COST,
            Candidate,
            prioritize,
        )

        candidates: list[Candidate] = []
        seen_candidates: set[int] = set()
        skipped_oversized = 0
        # Hydrate at most 200 candidates and their two graph ancestors in one
        # bounded query. Each group still passes its own eligibility screen;
        # hydration never makes foreign or unsafe evidence deliverable.
        prefetch_ids = dict.fromkeys(source for tid in stream[:MAX_CANDIDATES] for source in evidence_ids(tid))
        store.turns(prefetch_ids)
        for tid in stream[:MAX_CANDIDATES]:
            if tid in seen_candidates:
                continue
            seen_candidates.add(tid)
            evidence = evidence_group(tid)
            if not evidence or about_user_only and evidence[tid].role not in ("user", ""):
                continue
            passages = tuple(line_of(turn).removeprefix(turn.speaker + ": ") for turn in evidence.values())
            cost = evidence_cost(evidence)
            if sum(map(len, passages)) > MAX_PASSAGE_CHARS or cost > MAX_TOKEN_COST:
                skipped_oversized += 1
                continue
            candidates.append(Candidate(tid, passages, max(1, cost)))
        try:
            anchor_count = opts.primary_hits if opts.primary_hits is not None else 3
            plan = prioritize(question, candidates, anchor_count=min(MAX_CANDIDATES, max(0, anchor_count)))
        except ValueError as exc:
            raise ConversationError(str(exc)) from exc
        planned = set(plan.order)
        stream = list(plan.order) + [tid for tid in stream if tid not in planned]
        priority_quota = len(plan.priority)
        if selection is not None:
            selection.update(plan.as_dict())
            selection["skipped_oversized_groups"] = skipped_oversized

    with_context = opts.neighbour_hits if opts.neighbour_hits is not None else max(5, budget // 400)
    if summary and opts.neighbour_hits is None:
        with_context = len(stream)  # a summary reads each ask together with its answer
    # the best hits go in first, on their own: context around one hit must never push a
    # better-ranked hit out of the budget
    primary: set[int] = set()
    primary_hits = opts.primary_hits if opts.primary_hits is not None else 3
    primary_hits = max(primary_hits, priority_quota)
    head = store.turns(stream[:primary_hits * 3])
    for tid in stream[:primary_hits * 3]:
        turn = head.get(tid)
        if turn is None or len(primary) >= primary_hits:
            continue
        if about_user_only and turn.role not in ("user", ""):
            continue
        if _is_flagged(turn):
            if tid not in withheld:
                withheld.append(tid)
            continue
        evidence = evidence_group(tid)
        if not evidence:
            continue
        cost = evidence_cost(evidence)
        if spent + cost > budget:
            continue
        chosen.update(evidence)
        primary.add(tid)
        sessions_seen.update(t.session for t in evidence.values())
        spent += cost
    hits = 0
    for start in range(0, len(stream), 50):
        batch = stream[start:start + 50]
        turns = store.turns(batch)
        full = False
        for tid in batch:
            turn = turns.get(tid)
            if turn is None or (allowed is not None and tid not in allowed) \
                    or (tid in chosen and tid not in primary):
                continue
            first = tid in primary
            primary.discard(tid)
            if about_user_only and turn.role not in ("user", ""):
                continue
            if tid in parents and tid not in chosen:
                evidence = evidence_group(tid)
                cost = evidence_cost(evidence)
                if not evidence or spent + cost > budget:
                    continue
                chosen.update(evidence)
                sessions_seen.update(t.session for t in evidence.values())
                spent += cost
                # Neighbours are optional; supporting graph evidence is not.
                continue
            hits += 1
            if broad:
                user_req = summary and turn.role in ("user", "") and hits <= with_context
                near = store.neighbours(turn, 0, 1) if user_req else []
            else:
                near = store.neighbours(turn, opts.neighbours_before, opts.neighbours_after) \
                    if hits <= with_context else []
            group = [tid] + [n for n in near if allowed is None or n in allowed]
            if opts.bridge_turns > 0 and hits <= with_context and not broad:
                group += [b for b in _bridge(store, turn, chosen, opts.bridge_turns)
                          if b not in group and (allowed is None or b in allowed)]
            # A graph discovery enters through its complete evidence group,
            # rather than as an incidental neighbour with missing ancestors.
            group = [g for g in group if g == tid or g not in parents or g in chosen]
            group_turns = store.turns(group)
            # anti-recursion holds for neighbours too: a turn restating the
            # question is never context for its own answer, however adjacent.
            group = [g for g in group
                     if g == tid or g not in group_turns
                     or _normalize_text(group_turns[g].text) != question_norm]
            if opts.neighbour_minutes is not None and turn.at is not None:
                # a neighbour is context only when it was said close to the hit: turns of one
                # dialogue share a moment, separate notes made hours apart on one day do not
                gap = dt.timedelta(minutes=opts.neighbour_minutes)
                anchor_at: dt.datetime = turn.at
                group = [g for g in group if g == tid or g not in group_turns
                         or (neighbour_at := group_turns[g].at) is None or abs(neighbour_at - anchor_at) <= gap]
            for g in list(group):
                if g in group_turns and _is_flagged(group_turns[g]):
                    group.remove(g)
                    if g not in withheld:
                        withheld.append(g)
            if tid not in group:
                continue
            cost = sum(tokens(line_of(group_turns[g])) + 1 for g in group if g not in chosen and g in group_turns)
            if turn.session not in sessions_seen:
                cost += header_cost(turn.session, turn.at)
            if spent + cost > budget:
                if first:
                    continue  # already placed; its neighbours do not fit
                solo = tokens(line_of(turn)) + 1 + (0 if turn.session in sessions_seen else
                                                  header_cost(turn.session, turn.at))
                if spent + solo > budget:
                    full = spent >= budget * 0.97
                    continue
                group, cost = [tid], solo
            for g in group:
                if g in group_turns:
                    chosen[g] = group_turns[g]
            sessions_seen.add(turn.session)
            spent += cost
        if full:
            break
    by_session: dict[str, list[Turn]] = {}
    for turn in sorted(chosen.values(), key=lambda t: (t.at or dt.datetime.min, t.session, t.idx)):
        by_session.setdefault(turn.session, []).append(turn)
    blocks = []
    delivered = [profile_emissions[item] for item in instruction_lines + profile_lines]
    for session, session_turns in sorted(by_session.items(), key=lambda kv: (kv[1][0].at or dt.datetime.min, kv[0])):
        lines = [_header(session, session_turns[0].at, now)]
        if session in summaries:
            lines.append(_summary_line(summaries[session]["text"]))
            delivered.append(_EmittedText(summaries[session]["text"]))
        previous: int | None = None
        for displayed_turn in sorted(session_turns, key=lambda t: t.idx):
            if previous is not None and displayed_turn.idx > previous + 1:
                lines.append("…")
            lines.append(line_of(displayed_turn))
            delivered.append(_EmittedText(line_of(displayed_turn).removeprefix(displayed_turn.speaker + ": "),
                                          displayed_turn.speaker))
            previous = displayed_turn.idx
        blocks.append("\n".join(lines))
    context = profile_block + "\n\n".join(blocks)
    if not chosen and ranked and budget > 0 and ranked[0] not in parents:
        # A tiny allowance can omit a header, but never exceed the budget.
        top = store.turns(ranked[:1])
        if ranked[0] in top and (allowed is None or ranked[0] in allowed) and not _is_flagged(top[ranked[0]]):
            snippet = _excerpt(top[ranked[0]], question, budget)
            prefix = top[ranked[0]].speaker + ": "
            displayed_speaker = top[ranked[0]].speaker
            if tokens(snippet) > budget:
                snippet = snippet.removeprefix(prefix)
                displayed_speaker = ""
            snippet = snippet[:budget * 4]
            chosen[ranked[0]] = top[ranked[0]]
            context = snippet
            pinned = []
            body = snippet.removeprefix(prefix) if displayed_speaker else snippet
            if displayed_speaker and prefix.startswith(snippet):
                body, displayed_speaker = "", ""
            delivered = [_EmittedText(body, displayed_speaker)]
    if emitted is not None:
        emitted.extend(delivered)
    return context, list(chosen) + [t for t in dict.fromkeys(pinned) if t not in chosen], tokens(context)
