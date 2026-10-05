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
- owner is the named person or entity the memory is about. Keep different
  people's facts separate even when their slot is the same.
- source_turn_ids must contain the message_id numbers of the NEW MESSAGES
  that directly support this memory. Never invent a message_id.

KNOWN memories:
{known}

NEW MESSAGES (observed {observed}):
{messages}

Reply with JSON only: {{"memories": [{{"text": "...", "kind": "...", "slot": null,
"owner": "...", "source_turn_ids": [1]}}]}}"""


def _parse(text: str) -> list[dict]:
    from commontrace.llm import _extract_json_object

    try:
        data = _extract_json_object(text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ConversationError("memory extraction returned invalid JSON; checkpoint was not advanced") from exc
    memories = data.get("memories") if isinstance(data, dict) else None
    if not isinstance(memories, list) or any(not isinstance(m, dict) for m in memories):
        raise ConversationError("memory extraction must return a memories list; checkpoint was not advanced")
    return memories


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
            relevant = store.recall_facts(" ".join(t.text[:256] for t in batch), limit=KNOWN)
            known = "\n".join(f"- [{f['owner']}:{f['slot'] or '-'}] {f['statement']}"
                              for f in relevant[-KNOWN:]) or "(none)"
            messages = "\n".join(f"[message_id={t.id}] {t.speaker}: {t.annotated()}" for t in batch)
            text, _usage = complete(PROMPT.format(
                observed=observed.date().isoformat() if observed else "unknown",
                kinds=", ".join(FACT_KINDS), known=known, messages=messages))
            calls += 1
            memories = []
            by_id = {t.id: t for t in batch}
            default_owner = next((t.speaker for t in reversed(batch) if t.role == "user"), batch[-1].speaker)
            for m in _parse(text):
                if injection_guard.injection_labels({"text": str(m.get("text") or "")}):
                    refused += 1
                    continue
                source_ids = m.get("source_turn_ids", [t.id for t in batch])
                if not isinstance(source_ids, list) or not source_ids \
                        or any(not isinstance(tid, int) or isinstance(tid, bool) or tid not in by_id
                               for tid in source_ids):
                    raise ConversationError("extracted memory cites a message outside its batch")
                observed_at = max((by_id[tid].at for tid in source_ids if by_id[tid].at), default=None)
                memories.append({**m, "source_turn_ids": source_ids,
                                 "owner": m.get("owner") or default_owner,
                                 "at": observed_at.isoformat(timespec="minutes") if observed_at else None})
            added += store.add_memories(session, memories, source="model", extracted_through=batch[-1].idx)
    return {"space": store.space, "memories": added, "calls": calls, "refused": refused}
