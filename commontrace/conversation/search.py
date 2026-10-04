"""Recall: the turns that answer a question, assembled into a dated, token-budgeted
context. Lexical and semantic arms are fused per sub-query and combined by their
best rank (a turn that is first for one facet keeps that), a question's time
window lifts the turns said or set in it, and the page is filled best-first with
each hit's neighbouring turns, then shown in the order things were said."""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field

from commontrace import injection_guard
from commontrace.conversation import profile, timeparse
from commontrace.conversation.store import Store, Turn

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


def _core_topic(question: str) -> str:
    cleaned = _QUERY_FRAME.sub("", question.strip())
    cleaned = _QUERY_TRAILING.sub("", cleaned).strip(" ?,.:;")
    cleaned = re.sub(r"^(?:the\s+|about\s+|and\s+|my\s+|our\s+|how\s+)+", "", cleaned, flags=re.I)
    cleaned = re.sub(
        r"^(?:the\s+)?(?:order in which|sequence of|different aspects of|aspects of|timeline of|history of|"
        r"all the times i|details about)\s+", "", cleaned, flags=re.I,
    )
    cleaned = re.sub(r"^(?:i brought up|we talked about|we discussed|i mentioned)\s+", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^(?:different aspects of|aspects of)\s+", "", cleaned, flags=re.I)
    return cleaned.strip(" ?,.:;")


def subqueries(question: str) -> list[str]:
    """The question, plus each clause of a compound one, plus each aspect of a list
    ("a summary of X, including A, B and C" also searches "X A", "X B", "X C")."""
    out = [question]
    core = _core_topic(question)
    q_norm = question.rstrip(" ?,.:;").lower()
    if core and core.lower() != q_norm and len(core) >= 4:
        out.append(core)
    parts = re.split(r"\s*(?:;|,\s*and\b|\band then\b|\balso\b)\s*", question)
    if len(parts) > 1:
        out += [p for p in parts if len(re.findall(r"[A-Za-z]{3,}", p)) >= 2]
    coord = re.search(
        r"\b([a-z0-9_-]+(?:\s+[a-z0-9_-]+)?)\s+(?:or|and)\s+([a-z0-9_-]+(?:\s+[a-z0-9_-]+)?)\b",
        core or question, re.I
    )
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


def _embedder(store: Store, choice: str | None):
    from commontrace.conversation import embed

    tag = embed.configured() if choice == "auto" else choice
    if not tag:
        return None
    if not embed.available():
        return None
    return embed.Embedder(store.root, tag, read_only=getattr(store, "read_only", False))


def _header(session: str, at: dt.datetime | None) -> str:
    if at is None:
        return f"[{session}]"
    when = f"{timeparse.WEEKDAYS[at.weekday()].capitalize()} {timeparse.label(at.date())}"
    if at.hour or at.minute:
        when += at.strftime(", %H:%M")
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


def recall(store: Store, question: str, *, now=None, options: Options | None = None,
           extra_queries: list[str] = ()) -> Recall:
    """`extra_queries` are searched beside the question, each keeping its own best
    ranks (a follow-up search that finds a missing fact first is not diluted)."""
    from commontrace import telemetry

    with telemetry.span("conversation.recall", space=store.space, queries=1 + len(extra_queries)) as handle:
        result = _recall(store, question, now=now, options=options, extra_queries=extra_queries)
        handle.set(tokens=result.tokens, turns=len(result.turns))
        return result


def _recall(store: Store, question: str, *, now=None, options: Options | None = None,
            extra_queries: list[str] = ()) -> Recall:
    opts = options or Options()
    question = (question or "").strip()
    if not question:
        return Recall(question, "", 0, [], [], None)
    moment = timeparse.parse_moment(now) if isinstance(now, str) else now
    moment = moment or store.latest_moment()
    window = timeparse.question_window(question, moment)
    embedder = _embedder(store, opts.embedder)
    qvec_cache: dict[str, object] = {}
    allowed = store.allowed(sessions=opts.sessions, speakers=opts.speakers, since=opts.since,
                            until=opts.until, now=moment)
    pool = opts.pool if allowed is None else opts.pool * 10

    def arm_rankings(query: str) -> list[tuple[list[int], float]]:
        lexical = [u for u, _s in store.lexical(query, pool)]
        if embedder is None:
            return [(lexical, 1.0)]
        from commontrace.conversation import embed

        if query not in qvec_cache:
            qvec_cache[query] = embedder.encode([query], query=True)[0]
        dense = [u for u, _s in embed.search(store, embedder, qvec_cache[query], pool)]
        return [(dense, 1.0), (lexical, opts.lexical_weight)]

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

    queries = list(dict.fromkeys(subqueries(question) + [q.strip() for q in extra_queries if q and q.strip()]))
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
        if named:
            top = max(scores.values())
            boosted = 0
            for name, turns in store.entity_turns(named).items():
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
        order = sorted(scores, key=lambda t: t)  # turn ids grow with time of writing
        for position, turn in enumerate(order):
            scores[turn] += opts.recency_boost * top * (position / max(1, len(order) - 1))
        explain["recency"] = True
    asks_when = bool(_ASKS_WHEN.search(question or ""))
    if asks_when and scores:
        top = max(scores.values())
        top_candidates = list(scores.keys())[:100]
        dated_turns = set()
        for r in store.db.execute(
            f"SELECT id FROM turns WHERE id IN ({','.join('?' for _ in top_candidates)}) AND dates != '[]'",
            top_candidates,
        ):
            dated_turns.add(r[0])
        for turn in dated_turns:
            scores[turn] += 0.4 * top
        explain["asks_when"] = True
    ranked = sorted(scores, key=lambda t: (-scores[t], t))
    rerank = opts.rerank
    if rerank == "auto":
        rerank = "cross-encoder" if embedder is not None else None
    if rerank and ranked:
        ranked = _rerank(store, question, ranked, rerank, opts.rerank_depth, explain, opts.rerank_blend)
        explain["rerank"] = rerank
    conf = confidence(store, question, ranked[:5])
    explain["confidence"] = conf
    if conf == 0.0:
        explain["abstain"] = True
    withheld: list[int] = []
    context, used, n_tokens = assemble(store, question, ranked, opts, withheld, allowed)
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
    return order + [t for t in ranked if t not in set(order)]


_GLOBAL_RULE = re.compile(r"\b(?:format\w*|style|length|short|shorter|concise|brief|bullet\w*|list|language|units?|"
                          r"metric|imperial|code|snippets?|syntax|tone|formal|casual|words?|examples?|step|steps|"
                          r"explain\w*|answers?|responses?|repl(?:y|ies)|summar\w*|cite|sources?|emoji\w*)\b", re.I)


def _instruction_lines(store: Store, limit: int, question: str = "") -> list[tuple[str, int]]:
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
    for f in reversed(store.facts(["instruction"])):
        key = f["subject"]
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


def _profile_lines(store: Store, question: str, limit: int) -> list[tuple[str, int]]:
    if limit <= 0:
        return []
    facts = [f for f in store.facts() if f["kind"] != "instruction"]
    if not facts:
        return []
    asked = _stems(question)
    advice = bool(_ADVICE.search(question))
    about_self = bool(_ABOUT_SELF.search(question))
    preference = bool(_PREFERENCE.search(question))
    scored = []
    for f in facts:
        overlap = len(asked & _stems(f["statement"])) + (2 if about_self and f["kind"] == "identity" else 0)
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
        if f["statement"] in seen:
            continue
        seen.add(f["statement"])
        day = f"({f['at'][:10]}) " if f["at"] else ""
        out.append((f"- {day}{f['statement']}", f["turn"]))
        if len(out) >= limit:
            break
    return out


def _flagged(turn: Turn) -> bool:
    return bool(injection_guard.injection_labels({"text": turn.text}))


def assemble(store: Store, question: str, ranked: list[int], opts: Options,
             withheld: list[int] | None = None, allowed: set[int] | None = None) -> tuple[str, list[int], int]:
    """Fill the budget best-first, each hit with its neighbours, then render by time.
    A turn the injection screen flags is never shown; its id goes to `withheld`."""
    withheld = [] if withheld is None else withheld
    budget = max(0, opts.budget)
    instruction_lines = _instruction_lines(store, opts.instructions, question)
    instruction_block = ("[Standing instructions from the user]\n" + "\n".join(t for t, _ in instruction_lines)
                         + "\n\n") if instruction_lines else ""
    while instruction_lines and tokens(instruction_block) > budget // 6:
        instruction_lines.pop()
        instruction_block = ("[Standing instructions from the user]\n" + "\n".join(t for t, _ in instruction_lines)
                             + "\n\n") if instruction_lines else ""
    profile_lines = _profile_lines(store, question, opts.profile_facts)
    profile_block = ("[What the user has said about themselves]\n" + "\n".join(t for t, _ in profile_lines)
                     + "\n\n") if profile_lines else ""
    if tokens(profile_block) > budget // 4:
        profile_block, profile_lines = "", []
    profile_block = instruction_block + profile_block
    pinned = [turn for _t, turn in instruction_lines + profile_lines]
    spent = tokens(profile_block)
    chosen: dict[int, Turn] = {}
    sessions_seen: set[str] = set()
    summaries = store.summaries() if opts.summaries else {}

    def header_cost(session: str, at) -> int:
        cost = tokens(_header(session, at)) + 1
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

    def line_of(turn: Turn) -> str:
        if turn.id not in rendered:
            rendered[turn.id] = _excerpt(turn, question, cap)
        return rendered[turn.id]

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
        if _flagged(turn):
            if tid not in withheld:
                withheld.append(tid)
            continue
        cost = tokens(line_of(turn)) + 1 + (0 if turn.session in sessions_seen else header_cost(turn.session, turn.at))
        if spent + cost > budget:
            continue
        chosen[tid] = turn
        primary.add(tid)
        sessions_seen.add(turn.session)
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
            hits += 1
            if broad:
                user_req = summary and turn.role in ("user", "") and hits <= with_context
                near = store.neighbours(turn, 0, 1) if user_req else []
            else:
                near = store.neighbours(turn, opts.neighbours_before, opts.neighbours_after) \
                    if hits <= with_context else []
            group = [tid] + [n for n in near if allowed is None or n in allowed]
            group_turns = store.turns(group)
            if opts.neighbour_minutes is not None and turn.at is not None:
                # a neighbour is context only when it was said close to the hit: turns of one
                # dialogue share a moment, separate notes made hours apart on one day do not
                gap = dt.timedelta(minutes=opts.neighbour_minutes)
                group = [g for g in group if g == tid or g not in group_turns or group_turns[g].at is None
                         or abs(group_turns[g].at - turn.at) <= gap]
            for g in list(group):
                if g in group_turns and _flagged(group_turns[g]):
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
        lines = [_header(session, turns[0].at)]
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
    return context, list(chosen) + [t for t in dict.fromkeys(pinned) if t not in chosen], tokens(context)
