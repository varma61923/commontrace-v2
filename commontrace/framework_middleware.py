"""Native model hooks sharing one save-and-retrieve operation with the gateway.

Framework imports stay optional. Adapters accept the SDK's message containers;
callers pin the session and MemoryClient scopes when constructing middleware.
"""

from __future__ import annotations

import hashlib
import json

from commontrace import memory_guard
from commontrace.hook_kit import HookKit
from commontrace.memory_journal import Journal, moment, now
from commontrace.memory_record import MemoryRecord


def scope_from_labels(labels: list[str]) -> dict:
    result, extra = {}, []
    for label in labels:
        key, _, value = label.partition(":")
        if key in ("tenant", "user", "agent", "session", "repo") and value:
            if key in result and result[key] != value:
                raise ValueError("ambiguous memory scope dimension: " + key)
            result[key] = value
        else:
            extra.append(label)
    if extra:
        result["labels"] = sorted(extra)
    return result


def maintain_preferences(journal: Journal, text: str, scope: dict):
    """Explicit user preferences only; model/assistant assertions use a different author."""
    import re

    # Stable identity/occupation assertions are separate from decaying preferences.
    for match in re.finditer(r"\bI (?:work as|am a|am an)\s+([^.!?\n]{3,120})", text, flags=re.I):
        assertion = match.group(0).strip()
        identity = (
            "static-" + hashlib.sha256((json.dumps(scope, sort_keys=True) + assertion.casefold()).encode()).hexdigest()
        )
        if not journal.history(identity):
            journal.put(
                MemoryRecord(
                    identity,
                    "profile",
                    1,
                    {"text": assertion, "bucket": "static", "author": "user", "observed_at": now()},
                    scope=scope,
                    created_at=now(),
                    provenance={"authored_by": "user"},
                )
            )
    for match in re.finditer(r"\bI (?:prefer|like|always use|want)\s+([^.!?\n]{3,300})", text, flags=re.I):
        preference = match.group(0).strip()
        identity = (
            "preference-"
            + hashlib.sha256((json.dumps(scope, sort_keys=True) + preference.casefold()).encode()).hexdigest()
        )
        history = journal.history(identity)
        prior = history[-1].payload if history else {}
        journal.put(
            MemoryRecord(
                identity,
                "profile",
                len(history) + 1,
                {
                    "text": preference,
                    "bucket": "dynamic",
                    "weight": min(5.0, prior.get("weight", 0) + 1),
                    "half_life_days": 90,
                    "observed_at": now(),
                    "critical": False,
                    "author": "user",
                },
                scope=scope,
                created_at=now(),
                provenance={"authored_by": "user"},
            )
        )


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
    if text:
        kit.journal.capture(run, "UserMessage", {"text": text, "author": "user"}, scope=scope)
        maintain_preferences(kit.journal, text, scope)
    result = kit.handle(
        event or "UserPromptSubmit", {**(payload or {}), "session_id": session, "prompt": query, "budget": budget}
    )
    profile = {"static": [], "dynamic": [], "custom": []}
    for record in kit.journal.search("", scope=scope, kinds=("profile",), limit=30):
        data = record.payload
        age = max(0.0, (moment(now()) - moment(data.get("observed_at") or record.created_at or now())).total_seconds() / 86400)
        weight = data.get("weight", 1) * (
            1 if data.get("bucket") == "static" else 2 ** (-age / data.get("half_life_days", 90))
        )
        bucket = data.get("bucket", "custom")
        profile[bucket if bucket in profile else "custom"].append(
            {"id": record.id, "text": data.get("text", ""), "weight": weight}
        )
    buffer = kit.journal.buffer(run, scope=scope, limit=16)
    # The unextracted buffer is evidence, never silently promoted to a fact or rule.
    clean, _ = memory_guard.sanitize_metadata(buffer, pii=True)
    from commontrace import fact_store, search_recipes

    found = []
    if fact_store.enabled(root) and query.strip():
        found = search_recipes.search(root, query, context=context, limit=10)
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


def replace_memory_update(messages: list[dict], context: str) -> list[dict]:
    clean = list(messages)
    if (
        clean
        and isinstance(clean[-1], dict)
        and clean[-1].get("role") == "user"
        and is_memory_update(clean[-1].get("content"))
    ):
        clean.pop()
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
        query = str(
            messages[-1].get("content", "")
            if messages and isinstance(messages[-1], dict)
            else getattr(messages[-1], "content", "")
            if messages
            else state.get("input", "")
        )
        result = self.before_run(query, text=query)
        if result["context"]:
            messages.append({"role": "user", "content": result["context"]})
        return {"llm_input_messages": messages}

    def process_llm_request(self, callback_context, llm_request):
        contents = llm_request.contents
        if contents and getattr(contents[-1], "role", None) == "user":
            parts = getattr(contents[-1], "parts", []) or []
            if len(parts) == 1 and is_memory_update(getattr(parts[0], "text", None)):
                contents.pop()
        query = str(getattr(contents[-1], "parts", "")) if contents else ""
        result = self.before_run(query, text=query)
        if result["context"]:
            from google.genai import types

            contents.append(types.Content(role="user", parts=[types.Part(text=result["context"])]))
        return None

    def pydantic_history_processor(self, ctx, messages):
        messages = list(messages)
        if messages:
            parts = getattr(messages[-1], "parts", [])
            if (
                len(parts) == 1
                and getattr(parts[0], "part_kind", "") == "user-prompt"
                and is_memory_update(getattr(parts[0], "content", None))
            ):
                messages.pop()
        query = str(messages[-1]) if messages else ""
        result = self.before_run(query)
        if not result["context"]:
            return messages
        from pydantic_ai.messages import ModelRequest, UserPromptPart

        return [*messages, ModelRequest(parts=[UserPromptPart(content=result["context"])])]

    def openai_input(self, messages: list[dict]) -> list[dict]:
        messages = replace_memory_update(messages, "")
        result = self.before_run(str(messages[-1].get("content", "")) if messages else "")
        return replace_memory_update(messages, result["context"])

    def crewai_before_kickoff(self, inputs: dict) -> dict:
        result = self.before_run(json.dumps(inputs))
        return {**inputs, "commontrace_memory": result["context"]}

    def openai_call_model_input_filter(self, data):
        """OpenAI Agents RunConfig.call_model_input_filter callback."""
        from dataclasses import replace

        from agents.run_config import ModelInputData

        incoming = data.model_data
        items = self.openai_input(list(incoming.input))
        return replace(incoming, input=items) if isinstance(incoming, ModelInputData) else incoming

    async def microsoft_agent_middleware(self, context, next):
        """Agent Framework agent middleware callback; pin one middleware per run."""
        messages = list(context.messages)
        if (
            messages
            and str(getattr(messages[-1], "role", "")) == "user"
            and is_memory_update(getattr(messages[-1], "text", None))
        ):
            messages.pop()
        context.messages = messages
        query = str(messages[-1]) if messages else ""
        result = self.before_run(query)
        if result["context"]:
            from agent_framework import Message

            context.messages = [*context.messages, Message(role="user", text=result["context"])]
        await next(context)
