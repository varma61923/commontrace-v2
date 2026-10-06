"""Recall: the turns that answer a question, assembled into a dated, token-budgeted
context. Lexical and semantic arms are fused per sub-query and combined by their
best rank (a turn that is first for one facet keeps that), a question's time
window lifts the turns said or set in it, and the page is filled best-first with
each hit's neighbouring turns, then shown in the order things were said."""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from commontrace import injection_guard
from commontrace.conversation import profile, timeparse
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
    primary_hits: int | None = None  # best hits placed before any neighbours (default 3)
    embedder: str | None = "auto"
    summaries: bool = True
    sessions: tuple[str, ...] = ()
    speakers: tuple[str, ...] = ()
    since: str | None = None
    until: str | None = None
    graph_hops: int | None = None  # None: adapt to relational clauses; 0 disables; maximum 2


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


def tokens(text: str) -> int:
    """The context's size in tokens, estimated the way most tokenizers land: ~4 chars each."""
    return max(1, math.ceil(len(text) / 4)) if text else 0


_BROAD = re.compile(
    r"\b(?:summar(?:y|ise|ize|ies)|overview|recap|progress(?:ed)?|evolv(?:e|ed)|over time|so far|timeline|"
    r"in (?:what|which) order|order in which|sequence|chronolog\w*|throughout|across (?:our|my|all|the|these|"
    r"different) (?:conversations?|sessions?|chats?|discussions?|requests?)|walk me through|history of|"
    r"all (?:the )?(?:times|things|steps|changes|features|issues)|every (?:time|change|step)|how many\b|"
    r"total (?:number|count|amount)|list (?:all|every))\b", re.I)


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
    r"\b(?:when|what (?:time|date|day|month|year)|how (?:long|many (?:days|weeks|months|years))|how much time)\b",
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
        origins = {}
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
    """How much of the question the best recalled turns cover (0..1): a low value means
    memory probably does not hold the answer, so the answerer should say so."""
    asked = {w for w in re.findall(r"[a-z0-9]+", question.lower()) if len(w) > 2 and w not in profile.STOPWORDS
             and w not in _QUESTION_WORDS}
    if not asked or not turn_ids:
        return 0.0
    turns = store.turns(turn_ids)
    if not turns:
        return 0.0

    salient = asked - _ATTRIBUTE_WORDS
    if salient:
        all_text = " ".join(f"{t.speaker} {t.annotated()} {t.at or ''}".lower() for t in turns.values())
        salient_found = any(w in all_text or (len(w) >= 4 and w[:4] in all_text) for w in salient)
        if not salient_found:
            return 0.0

    best = 0.0
    for t in turns.values():
        turn_str = f"{t.speaker} {t.annotated()} {t.at or ''}".lower()
        have = set(re.findall(r"[a-z0-9]+", turn_str))
        stems = {w[:5] for w in have}
        hit = sum(1 for w in asked if w in have or w[:5] in stems)
        best = max(best, hit / len(asked))
    return round(best, 3)


def is_broad(question: str) -> bool:
    """A question about a whole topic across sessions (a summary, an order of events, a
    count across conversations): it needs coverage more than the single best passage."""
    return bool(_BROAD.search(question or ""))


_ADVICE = re.compile(r"\b(?:recommend|suggest|suggestions?|ideas?|tips?|advice|should I|what should|"
                     r"any (?:good|other)|help me|how (?:can|could|should|would|do) I|how to|walk me through|"
                     r"can you (?:show|explain|help|give|write|create|make|plan)|explain|steps?|approach|"
                     r"what (?:libraries|tools|options|places|ways)|best way|plan(?:ning)? (?:my|a|the))\b", re.I)


_ABOUT_SELF = re.compile(r"\b(?:profession|occupation|job|career|for a living|my (?:work|role|background|name|age)|"
                         r"who am I|where (?:do|did) I (?:live|work|grow up)|how old am I)\b", re.I)


def _stems(text: str) -> set[str]:
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
    r"\b([a-z0-9_-]+(?:\s+[a-z0-9_-]+)?)\s+(?:or|and)\s+([a-z0-9_-]+(?:\s+[a-z0-9_-]+)?)\b",
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


def subqueries(question: str) -> list[str]:
    """The question, plus each clause of a compound one, plus each aspect of a list
    ("a summary of X, including A, B and C" also searches "X A", "X B", "X C")."""
    out = [question]
    core = _core_topic(question)
    q_norm = question.rstrip(" ?,.:;").lower()
    if core and core.lower() != q_norm and len(core) >= 4:
        out.append(core)
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
        topic = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", head)
                 if w.lower() not in profile.STOPWORDS and w.lower() not in _FRAME_WORDS][:4]
        for aspect in re.split(r",\s*(?:and\s+)?|\band\b|\bincluding\b|:", rest):
            words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", aspect) if w.lower() not in profile.STOPWORDS]
            if words:
                if len(words) >= 2:
                    out.append(" ".join(words))
                if topic:
                    out.append(" ".join(topic + words))
    return list(dict.fromkeys(out))[:12]


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
    if not tag:
        return None
    if not embed.available():
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


def _excerpt(turn: Turn, question: str, cap: int) -> str:
    """A long turn cut to the passages that bear on the question (in their original
    order, gaps marked), so one pasted log or long answer cannot crowd out the rest."""
    full = _line(turn)
    if tokens(full) <= cap:
        return full
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
    keep, spent = set(), tokens(turn.speaker) + 4
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
                extra_queries: list[str]) -> tuple | None:
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
        opts.neighbour_hits, opts.neighbour_minutes, opts.excerpt_tokens,
        opts.window_boost, opts.entity_boost, opts.lexical_weight, opts.rerank,
        opts.rerank_depth, opts.rerank_blend, opts.profile_facts, opts.instructions,
        opts.broad, opts.recency_boost, opts.primary_hits, opts.embedder,
        opts.summaries, opts.sessions, opts.speakers, opts.since, opts.until, opts.graph_hops,
        tuple(extra_queries), stamp,
    )


def recall(store: Store, question: str, *, now=None, options: Options | None = None,
           extra_queries: list[str] = ()) -> Recall:
    """`extra_queries` are searched beside the question, each keeping its own best
    ranks (a follow-up search that finds a missing fact first is not diluted).

    Repeat questions use a bounded LRU invalidated by local and external writes.
    Results are independent copies; closing the store releases its cached data.
    """
    from commontrace import telemetry

    with store.read_snapshot(), telemetry.span(
            "conversation.recall", space=store.space, queries=1 + len(extra_queries)) as handle:
        opts = options or Options()
        key = _recall_key(store, question, now, opts, extra_queries)
        if key is not None:
            with _RECALL_LOCK:
                hit = _RECALL_CACHE.get(key)
                if hit is not None:
                    _RECALL_CACHE.move_to_end(key)
                    handle.set(tokens=hit.tokens, turns=len(hit.turns), cached=True)
                    return copy.deepcopy(hit)
        result = _recall(store, question, now=now, options=options, extra_queries=extra_queries)
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
            extra_queries: list[str] = ()) -> Recall:
    opts = options or Options()
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
    embedder = _embedder(store, opts.embedder)
    until = opts.until
    if now is not None and moment is not None:
        parsed_until = timeparse.parse_moment(until) if until else moment
        if parsed_until is None:
            raise ConversationError(f"unrecognised date {until!r}")
        until = min(parsed_until, moment)
    allowed = store.allowed(sessions=opts.sessions, speakers=opts.speakers, since=opts.since,
                            until=until, now=moment if now is not None else None)
    pool = opts.pool
    queries = list(dict.fromkeys(subqueries(question) + [q.strip() for q in extra_queries if q and q.strip()]))
    dense_rankings: dict[str, list[int]] = {}
    if embedder is not None and allowed != set() and pool > 0:
        from commontrace.conversation import embed

        # Encode related facets together and scan each bounded query batch once.
        for start in range(0, len(queries), embed.QUERY_BATCH):
            batch = queries[start:start + embed.QUERY_BATCH]
            vectors = embedder.encode(batch, query=True)
            pages = embed.search_many(store, embedder, vectors, pool, allowed=allowed)
            dense_rankings.update((q, [u for u, _s in hits]) for q, hits in zip(batch, pages))

    def arm_rankings(query: str) -> list[tuple[list[int], float]]:
        lexical = [u for u, _s in store.lexical(query, pool, allowed=allowed)]
        if embedder is None:
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
        if not named:
            q_words = {w for w in re.findall(r"[a-z0-9_-]+", question.lower())
                       if len(w) >= 3 and w not in profile.STOPWORDS and w not in _QUESTION_WORDS}
            for w in q_words:
                found = store.db.execute(
                    "SELECT 1 FROM entities WHERE name=? UNION SELECT 1 FROM turns WHERE LOWER(speaker)=? LIMIT 1",
                    (w, w)
                ).fetchone()
                if found:
                    named.add(w)
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
        top = max(scores.values())
        candidates = store.turns(scores)
        order = sorted(scores, key=lambda t: (candidates[t].at or dt.datetime.min, t))
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
            scores[turn] += 0.4 * top
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
    context, used, n_tokens = assemble(store, question, ranked, opts, withheld, allowed, now=show_now,
                                     as_of=belief_at, current_instructions=now is None and belief_at is not None,
                                     evidence_paths=explain.get("graph_paths", ()))
    if explain.get("graph_paths"):
        chosen = set(used)
        explain["selected_graph_paths"] = [p for p in explain["graph_paths"]
                                           if p["source"] in chosen and p["turn"] in chosen]
    conf = confidence(store, question, used[:5]) if context else 0.0
    explain["confidence"] = conf
    if conf == 0.0:
        explain["abstain"] = True
    if withheld:
        explain["withheld"] = withheld
    return Recall(question, context, n_tokens, used, ranked,
                  (window[0].isoformat(), window[1].isoformat(), window[2]) if window else None, explain)


def _rerank(store: Store, question: str, ranked: list[int], mode: str, depth: int,
            explain: dict | None = None, blend: float = 0.0) -> list[int]:
    from commontrace import rerank_arm

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


_GLOBAL_RULE = re.compile(r"\b(?:format\w*|style|length|short|shorter|concise|brief|bullet\w*|list|language|units?|"
                          r"metric|imperial|code|snippets?|syntax|tone|formal|casual|words?|examples?|step|steps|"
                          r"explain\w*|answers?|responses?|repl(?:y|ies)|summar\w*|cite|sources?|emoji\w*)\b", re.I)


def _instruction_lines(store: Store, limit: int, question: str = "", *,
                       facts: list[dict] | None = None) -> list[tuple[str, int]]:
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
        out.append((f"- {day}{f['statement']}", f["turn"]))
    return out


def _profile_lines(store: Store, question: str, limit: int, *,
                   facts: list[dict] | None = None) -> list[tuple[str, int]]:
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
        out.append((f"- {day}{owner}: {f['statement']}", f["turn"]))
        if len(out) >= limit:
            break
    return out


def _flagged(turn: Turn) -> bool:
    return bool(injection_guard.injection_labels({"text": turn.text}))


def assemble(store: Store, question: str, ranked: list[int], opts: Options,
             withheld: list[int] | None = None, allowed: set[int] | None = None,
             now: dt.datetime | None = None, as_of=None,
             current_instructions: bool = False, evidence_paths=()) -> tuple[str, list[int], int]:
    """Fill the budget best-first, each hit with its neighbours, then render by time.
    A turn the injection screen flags is never shown; its id goes to `withheld`."""
    withheld = [] if withheld is None else withheld
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
    instruction_lines = _instruction_lines(store, opts.instructions, question, facts=facts)
    instruction_block = ("[Standing instructions from the user]\n" + "\n".join(t for t, _ in instruction_lines)
                         + "\n\n") if instruction_lines else ""
    while instruction_lines and tokens(instruction_block) > budget // 6:
        instruction_lines.pop()
        instruction_block = ("[Standing instructions from the user]\n" + "\n".join(t for t, _ in instruction_lines)
                             + "\n\n") if instruction_lines else ""
    profile_lines = _profile_lines(store, question, opts.profile_facts, facts=facts)
    profile_title = "What the user has said about themselves"
    if as_of is not None:
        profile_title += f"; beliefs as of {timeparse.parse_moment(as_of).isoformat()}"
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
    about_user_only = broad and bool(re.search(
        r"\b(?:i (?:brought up|raised|mentioned|asked|said|wanted)|my questions?)\b", question or "", re.I
    ))
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

    def evidence_group(tid):
        # A graph answer and its connecting passages are one evidence unit.
        # Keep at most the two traversed ancestors, with cycle protection.
        ids = [tid]
        for _ in range(2):
            parent = parents.get(ids[-1])
            if parent is None or parent in ids:
                break
            ids.append(parent)
        turns = store.turns(ids)
        norm = _normalize_text(question)
        for i in ids:
            t = turns.get(i)
            if t is None or (allowed is not None and i not in allowed) or _normalize_text(t.text) == norm:
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
        stream = diversified + [t for t in ranked if t not in set(diversified)]

    with_context = opts.neighbour_hits if opts.neighbour_hits is not None else max(5, budget // 400)
    # the best hits go in first, on their own: context around one hit must never push a
    # better-ranked hit out of the budget
    primary: set[int] = set()
    primary_hits = opts.primary_hits if opts.primary_hits is not None else 3
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
            if turn is None or (tid in chosen and tid not in primary):
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
            # A graph discovery enters through its complete evidence group,
            # rather than as an incidental neighbour with missing ancestors.
            group = [g for g in group if g == tid or g not in parents or g in chosen]
            group_turns = store.turns(group)
            # anti-recursion holds for neighbours too: a turn restating the
            # question is never context for its own answer, however adjacent.
            question_norm = _normalize_text(question)
            group = [g for g in group
                     if g == tid or g not in group_turns
                     or _normalize_text(group_turns[g].text) != question_norm]
            if opts.neighbour_minutes is not None and turn.at is not None:
                # a neighbour is context only when it was said close to the hit: turns of one
                # dialogue share a moment, separate notes made hours apart on one day do not
                gap = dt.timedelta(minutes=opts.neighbour_minutes)
                group = [g for g in group if g == tid or g not in group_turns or group_turns[g].at is None
                         or abs(group_turns[g].at - turn.at) <= gap]
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
    for session, turns in sorted(by_session.items(), key=lambda kv: (kv[1][0].at or dt.datetime.min, kv[0])):
        lines = [_header(session, turns[0].at, now)]
        if session in summaries:
            lines.append(_summary_line(summaries[session]["text"]))
        previous = None
        for turn in sorted(turns, key=lambda t: t.idx):
            if previous is not None and turn.idx > previous + 1:
                lines.append("…")
            lines.append(line_of(turn))
            previous = turn.idx
        blocks.append("\n".join(lines))
    context = profile_block + "\n\n".join(blocks)
    if not chosen and ranked and budget > 0 and ranked[0] not in parents:
        # A tiny allowance can omit a header, but never exceed the budget.
        top = store.turns(ranked[:1])
        if ranked[0] in top and (allowed is None or ranked[0] in allowed) and not _is_flagged(top[ranked[0]]):
            snippet = _excerpt(top[ranked[0]], question, budget)
            if tokens(snippet) > budget:
                snippet = snippet.removeprefix(top[ranked[0]].speaker + ": ")
            snippet = snippet[:budget * 4]
            chosen[ranked[0]] = top[ranked[0]]
            context = snippet
            pinned = []
    return context, list(chosen) + [t for t in dict.fromkeys(pinned) if t not in chosen], tokens(context)
