"""Native model hooks sharing one save-and-retrieve operation with the gateway.

Framework imports stay optional. Adapters accept the SDK's message containers;
callers pin the session and MemoryClient scopes when constructing middleware.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math

from commontrace import memory_guard
from commontrace.configuration import configured
from commontrace.hook_kit import HookKit
from commontrace.memory_journal import Journal, moment, now
from commontrace.memory_record import MemoryRecord, rerank_profile_results
from commontrace.record_visibility import scope_from_labels


def maintain_preferences(journal: Journal, text: str, scope: dict, *, source_ids=None):
    """Explicit user preferences only; model/assistant assertions use a different author."""
    from commontrace.memory_record import preference_candidates, preference_statistics

    for identity, content in preference_candidates(text, scope):
        previous = journal.latest(identity)
        prior = previous.payload if previous else {}
        data = {**content, "observed_at": now(), "source_message_ids": source_ids or []}
        if content["bucket"] == "dynamic":
            data.update(preference_statistics(prior))
        journal.put(
            MemoryRecord(
                identity,
                "profile",
                previous.version + 1 if previous else 1,
                data,
                scope=scope,
                created_at=now(),
                provenance={"authored_by": "user"},
            )
        )


@configured
def turn(
    root: str,
    session: str,
    query: str,
    *,
    context: list[str],
    text="",
    budget=600,
    embedder=None,
    event=None,
    payload=None,
) -> dict:
    from commontrace import api_schema

    api_schema.validate_request(
        "turn",
        {
            "session_id": session,
            "query": query,
            "text": text,
            "budget": budget,
            "context": context,
            **({"event": event, "payload": payload or {}} if event else {}),
        },
    )
    scope = scope_from_labels(context)
    agent = scope.get("agent", "local")
    scope["agent"] = agent
    kit = HookKit(root, agent, scope=scope, embedder=embedder)
    run = hashlib.sha256(json.dumps([scope, session], sort_keys=True).encode()).hexdigest()
    import uuid

    event_payload = {**(payload or {}), "session_id": session, "prompt": query, "budget": budget}
    event_payload.setdefault("event_id", uuid.uuid4().hex)
    if text:
        captured = kit.journal.capture(run, "UserMessage", {"text": text, "author": "user"}, scope=scope)
        maintain_preferences(kit.journal, text, scope, source_ids=[captured["id"], event_payload["event_id"]])
    # Framework turns are request/response operations, not the 50 ms native
    # editor hook. Allow bounded cold-store initialization and governed reads
    # to complete before discarding the entire context as a hook timeout.
    result = kit.handle(event or "UserPromptSubmit", event_payload, deadline_ms=1000)
    profile = {"static": [], "dynamic": [], "custom": []}
    preference_rankers = []
    for record in kit.journal.search("", scope=scope, kinds=("profile",), limit=30):
        data = record.payload
        half_life, initial_weight = data.get("half_life_days", 90), data.get("weight", 1)
        if (
            type(half_life) not in (int, float)
            or not 0 < half_life <= 36500
            or not math.isfinite(half_life)
            or type(initial_weight) not in (int, float)
            or not 0 <= initial_weight <= 10000
            or not math.isfinite(initial_weight)
        ):
            continue
        age = max(
            0.0, (moment(now()) - moment(data.get("observed_at") or record.created_at or now())).total_seconds() / 86400
        )
        weight = initial_weight * (1 if data.get("bucket") == "static" else 2 ** (-age / half_life))
        bucket = data.get("bucket", "custom")
        entry = {"id": record.id, "text": data.get("text", ""), "weight": weight}
        profile[bucket if bucket in profile else "custom"].append(entry)
        if (
            bucket == "dynamic"
            and data.get("author") == "user"
            and record.provenance.get("authored_by") == "user"
            and isinstance(data.get("source_message_ids"), list)
            and any(isinstance(identity, str) and identity for identity in data["source_message_ids"])
        ):
            preference_rankers.append({**entry, "bucket": bucket, "author": "user"})
    buffer = kit.journal.buffer(run, scope=scope, limit=16)
    # The unextracted buffer is evidence, never silently promoted to a fact or rule.
    clean, _ = memory_guard.sanitize_metadata(buffer, pii=True)
    from commontrace import search_recipes

    found = []
    if query.strip():
        found = search_recipes.search(root, query, context=context, limit=10)
        found = rerank_profile_results(found, preference_rankers)
    return {
        "context": result["context"],
        "injected": result["injected"],
        "profile": profile,
        "buffer": clean,
        "session_id": session,
        "scope": scope,
        "results": found,
    }


def is_memory_update(text) -> bool:
    import re

    return isinstance(text, str) and bool(re.fullmatch(r"\s*<memory_update>.*</memory_update>\s*", text, re.S))


def native_user_text(message) -> str:
    """Explicit human authorship across SDK containers; assistant/tool echoes stay out of profiles."""

    def text_content(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(
                part["text"]
                for part in value
                if isinstance(part, dict)
                and part.get("type") in ("text", "input_text")
                and isinstance(part.get("text"), str)
            )
        return ""

    if isinstance(message, dict):
        return text_content(message.get("content")) if message.get("role") == "user" else ""
    role = str(getattr(message, "role", getattr(message, "type", "")))
    parts = getattr(message, "parts", []) or []
    if role in ("user", "human"):
        for name in ("content", "text"):
            value = getattr(message, name, None)
            if isinstance(value, (str, list)):
                return text_content(value)
        return "\n".join(part.text for part in parts if isinstance(getattr(part, "text", None), str))
    # Pydantic ModelRequest has no role; UserPromptPart still has explicit authorship.
    return "\n".join(
        part.content
        for part in parts
        if getattr(part, "part_kind", "") == "user-prompt" and isinstance(getattr(part, "content", None), str)
    )


def user_query(messages) -> str:
    return next(
        (text for message in reversed(messages) if (text := native_user_text(message)) and not is_memory_update(text)),
        "",
    )


def strip_memory_updates(messages):
    return [message for message in messages if not is_memory_update(native_user_text(message))]


def replace_memory_update(messages: list[dict], context: str) -> list[dict]:
    clean = strip_memory_updates(messages)
    return [*clean, {"role": "user", "content": context}] if context else clean


class Middleware:
    def __init__(self, memory, session_id: str, *, budget=600):
        self.memory, self.session_id, self.budget = memory, session_id, budget
        self.scope = scope_from_labels(memory.context)
        self.scope.setdefault("agent", memory.agent_id or "local")
        self.run_key = hashlib.sha256(json.dumps([self.scope, session_id], sort_keys=True).encode()).hexdigest()

    def before_run(self, query: str, *, text="") -> dict:
        return self.memory.turn(self.session_id, query, text=text, budget=self.budget)

    def after_run(self, outcome: dict) -> dict:
        # A local adapter captures explicit results; HTTP adapters use the same turn API.
        if self.memory.url:
            return self.memory.turn(self.session_id, "", event="SessionEnd", payload=outcome, budget=0)
        return HookKit(self.memory.root, self.memory.agent_id or "local", scope=self.scope).handle(
            "SessionEnd", {"session_id": self.session_id, **outcome}
        )

    def langgraph_pre_model(self, state: dict, config=None) -> dict:
        messages = replace_memory_update(list(state.get("messages", [])), "")
        query = user_query(messages)
        result = self.before_run(query, text=query)
        if result["context"]:
            messages.append({"role": "user", "content": result["context"]})
        return {"llm_input_messages": messages}

    def process_llm_request(self, callback_context, llm_request):
        contents = strip_memory_updates(llm_request.contents)
        llm_request.contents = contents
        query = user_query(contents)
        result = self.before_run(query, text=query)
        if result["context"]:
            from google.genai import types

            contents.append(types.Content(role="user", parts=[types.Part(text=result["context"])]))
        return None

    def pydantic_history_processor(self, ctx, messages):
        messages = strip_memory_updates(messages)
        query = user_query(messages)
        result = self.before_run(query, text=query)
        if not result["context"]:
            return messages
        from pydantic_ai.messages import ModelRequest, UserPromptPart

        return [*messages, ModelRequest(parts=[UserPromptPart(content=result["context"])])]

    def openai_input(self, messages: list[dict]) -> list[dict]:
        messages = replace_memory_update(messages, "")
        query = user_query(messages)
        result = self.before_run(query, text=query)
        return replace_memory_update(messages, result["context"])

    def crewai_before_kickoff(self, inputs: dict) -> dict:
        if not isinstance(inputs, dict):
            return inputs
        clean = {key: value for key, value in inputs.items() if key != "commontrace_memory"}
        try:
            result = self.before_run(json.dumps(clean, ensure_ascii=False, sort_keys=True))
            context = result.get("context", "")
            return {**clean, "commontrace_memory": context} if isinstance(context, str) else clean
        except Exception:
            logging.getLogger(__name__).debug(
                "CrewAI memory retrieval failed; continuing without injected context", exc_info=True
            )
            return clean

    def crewai_after_kickoff(self, output, *, succeeded: bool = True):
        """Capture a completed CrewAI result and return it unchanged.

        The default ``succeeded`` value records that kickoff returned normally;
        it does not claim that the application-level result was useful or correct.
        """
        if type(succeeded) is not bool:
            return output
        try:
            result = getattr(output, "raw", output)
            if not isinstance(result, str):
                result = str(result)
            self.after_run({"succeeded": succeeded, "result": result[:20_000]})
        except Exception:
            logging.getLogger(__name__).debug(
                "CrewAI result capture failed; returning the original output", exc_info=True
            )
        return output

    def openai_call_model_input_filter(self, data):
        """OpenAI Agents RunConfig.call_model_input_filter callback."""
        from dataclasses import replace

        from agents.run_config import ModelInputData

        incoming = data.model_data
        items = self.openai_input(list(incoming.input))
        return replace(incoming, input=items) if isinstance(incoming, ModelInputData) else incoming

    async def microsoft_agent_middleware(self, context, next):
        """Agent Framework agent middleware callback; pin one middleware per run."""
        messages = strip_memory_updates(context.messages)
        context.messages = messages
        query = user_query(messages)
        result = self.before_run(query, text=query)
        if result["context"]:
            from agent_framework import Message

            context.messages = [*context.messages, Message(role="user", text=result["context"])]
        await next(context)
