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

ANSWER = """You answer from your memory of past conversations with the user.
Use only the memories below. Session headers say when each session took place; dates in
[brackets] resolve relative time words.

How to answer:
- Follow every standing instruction the user gave (format, length, style, things to avoid),
  and shape suggestions to the preferences they stated, even when the question does not
  mention them.
- If something changed over time, the most recent statement is current; say what it was
  before only if asked.
- If two memories contradict each other and neither is clearly the later correction, say
  that the memories conflict, quote both briefly, and ask which is right instead of picking one.
- If the memories do not contain the answer, say you don't have that information; never guess
  a detail that was not stated.
- For "when", "how long" and "how many days/weeks" questions, compute from the dates shown.
- For order or sequence questions, list the items in the order they happened or were raised,
  with their dates.
- For summaries, cover the whole span in chronological order, not only the latest part.
Answer concisely.

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
    ctx_text = result.context or "(no memories)"
    if result.explain.get("abstain"):
        ctx_text += (
            "\n\n[Note: No mentions of this subject were found in memory. If asking for a specific detail "
            "that was never recorded, state that you do not have this information.]"
        )
    text, used = complete(ANSWER.format(context=ctx_text, question=question, now=when))
    usage.append(used)
    return {"question": question, "answer": text.strip(), "context": result.context, "tokens": result.tokens,
            "follow_ups": asked, "rounds": searched, "usage": usage, "turns": result.turns,
            "explain": result.explain}
