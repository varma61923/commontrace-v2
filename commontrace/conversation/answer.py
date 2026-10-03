"""Answer a question from conversation memory with the configured model.

`rounds` > 1 is multi-round recall, after EverOS's decider loop: the model reads what
was recalled and either says it is enough or names what is missing as follow-up
searches; each follow-up is searched on its own and keeps its best ranks, so a fact
only one follow-up finds is not drowned by the rest."""
from __future__ import annotations

import json

from commontrace.conversation import timeparse
from commontrace.conversation.search import Options, Recall, recall
from commontrace.conversation.store import Store

MAX_ROUNDS = 4
MAX_FOLLOW_UPS = 3

ANSWER = """You answer questions from your memory of past conversations.
Use only the memories below. Dates in [brackets] say when relative time words
happened; session headers say when each session took place. When facts changed over
time, the most recent one is current. Answer in a short phrase. If the memories do
not contain the answer, say you don't know.

Memories:
{context}

Question (asked {now}): {question}
Answer:"""

DECIDE = """You are deciding whether the memories below are enough to answer a question.
If they are, reply {{"done": true}}. If something is missing, reply with up to {n}
short search queries for the missing pieces, each about one person, event or thing:
{{"done": false, "queries": ["...", "..."]}}. Reply with JSON only.

Question: {question}

Memories:
{context}"""


def _follow_ups(text: str) -> list[str] | None:
    from commontrace.llm import _extract_json_object

    try:
        data = _extract_json_object(text)
    except (ValueError, json.JSONDecodeError):
        return None
    if data.get("done") is True:
        return None
    queries = data.get("queries")
    if not isinstance(queries, list):
        return None
    return [str(q).strip()[:300] for q in queries if str(q).strip()][:MAX_FOLLOW_UPS] or None


def answer(store: Store, question: str, *, now=None, options: Options | None = None, rounds: int = 1,
           complete=None) -> dict:
    from commontrace import llm

    complete = complete or llm.complete
    rounds = max(1, min(int(rounds), MAX_ROUNDS))
    result: Recall = recall(store, question, now=now, options=options)
    asked: list[str] = []
    usage: list[dict] = []
    searched = 1
    for _round in range(rounds - 1):
        reply, used = complete(DECIDE.format(question=question, context=result.context or "(nothing)",
                                             n=MAX_FOLLOW_UPS))
        usage.append(used)
        follow = [q for q in (_follow_ups(reply) or []) if q not in asked]
        if not follow:
            break
        asked += follow
        searched += 1
        result = recall(store, question, now=now, options=options, extra_queries=asked)
    moment = timeparse.parse_moment(now) if isinstance(now, str) else now
    moment = moment or store.latest_moment()
    when = timeparse.label(moment.date()) if moment else "now"
    text, used = complete(ANSWER.format(context=result.context or "(no memories)", question=question, now=when))
    usage.append(used)
    return {"question": question, "answer": text.strip(), "context": result.context, "tokens": result.tokens,
            "follow_ups": asked, "rounds": searched, "usage": usage, "turns": result.turns}
