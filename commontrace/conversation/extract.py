"""Model-distilled memories, adapted from mem0's additive extraction: one call per
batch of new messages turns them into self-contained, dated statements, linked to
what is already known so a newer statement of the same thing replaces the older.
Optional: recall works without it, and nothing here runs unless asked."""
from __future__ import annotations

import json

from commontrace import injection_guard
from commontrace.conversation.store import FACT_KINDS, ConversationError, Store

BATCH = 40
KNOWN = 30

PROMPT = """You extract memories from a conversation so an assistant can answer questions
about it months later. Extract every memorable fact from the NEW MESSAGES: who people
are, what they did, plan, own, like and dislike, events with their dates, and what
the assistant recommended or agreed to.

Rules:
- Each memory is one self-contained sentence naming the person ("Caroline", "the
  user"), never "I" or "he".
- Ground every time reference to an absolute date using the observation date
  ({observed}). Dates in [brackets] are already resolved; keep them.
  "went to Paris last week" -> "went to Paris the week before 8 May 2023".
- Do not repeat a KNOWN memory. If a new message changes a known fact (a new job,
  a new home, a changed preference), write the new fact and give it the same "slot"
  as the fact it replaces.
- kind is one of: {kinds}.
- slot is a short key for single-valued facts (job, home, partner, favorite:<thing>),
  otherwise null.

KNOWN memories:
{known}

NEW MESSAGES (observed {observed}):
{messages}

Reply with JSON only: {{"memories": [{{"text": "...", "kind": "...", "slot": null}}]}}"""


def _parse(text: str) -> list[dict]:
    from commontrace.llm import _extract_json_object

    try:
        data = _extract_json_object(text)
    except (ValueError, json.JSONDecodeError):
        return []
    memories = data.get("memories") if isinstance(data, dict) else None
    return [m for m in memories if isinstance(m, dict)] if isinstance(memories, list) else []


def extract(store: Store, sessions: list[str] | None = None, *, complete=None) -> dict:
    """Distil memories from every message not yet distilled, session by session."""
    from commontrace import llm

    complete = complete or llm.complete
    targets = sessions or [s["id"] for s in store.sessions()]
    added = calls = refused = 0
    for session in targets:
        turns = store.session_turns(session)
        if not turns:
            raise ConversationError(f"no session {session!r} in space {store.space!r}")
        done = int(store.get_meta(f"extracted:{session}") or -1)
        todo = [t for t in turns if t.idx > done]
        for start in range(0, len(todo), BATCH):
            batch = todo[start:start + BATCH]
            observed = (batch[0].at or batch[-1].at)
            known = "\n".join(f"- [{f['slot'] or '-'}] {f['statement']}" for f in store.facts()[-KNOWN:]) or "(none)"
            messages = "\n".join(f"{t.speaker}: {t.annotated()}" for t in batch)
            text, _usage = complete(PROMPT.format(
                observed=observed.date().isoformat() if observed else "unknown",
                kinds=", ".join(FACT_KINDS), known=known, messages=messages))
            calls += 1
            memories = []
            for m in _parse(text):
                if injection_guard.injection_labels({"text": str(m.get("text") or "")}):
                    refused += 1
                    continue
                memories.append({**m, "at": batch[-1].at.isoformat(timespec="minutes") if batch[-1].at else None})
            added += store.add_memories(session, memories, source="model")
            store.set_meta(f"extracted:{session}", str(batch[-1].idx))
    return {"space": store.space, "memories": added, "calls": calls, "refused": refused}
