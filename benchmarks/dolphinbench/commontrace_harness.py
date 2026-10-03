"""DolphinBench harness: an agent whose long-term memory is CommonTrace.

Ingestion writes every dated history message into a per-persona CommonTrace
conversation store as it arrives (no model is needed for the memory itself; the
benchmark still requires one recorded assistant reply per message, kept to a few
tokens). `freeze` embeds every unit, checkpoints the SQLite files and returns their
hashes; tests open that frozen store read-only, so no test can change what the next
one remembers.

At test time the agent receives the request, the memories CommonTrace recalls for it,
the simulated app tools, and a `search_memory` tool for follow-up lookups (a person,
a project, a standing preference), and runs a tool loop against the configured
model.

Use it from a DolphinBench checkout:

    cp path/to/commontrace/benchmarks/dolphinbench/commontrace_harness.py my_harness.py
    # run.yaml: adapter: my_harness:BenchmarkHarness, options: see DEFAULT_OPTIONS
    python -m harness.runner prepare
    python -m harness.runner ingest --confirm-paid-calls
    python -m harness.runner evaluate --confirm-paid-calls

Model access is OpenAI-compatible chat completions (OpenAI, Azure OpenAI, OpenRouter,
vLLM, ...) or the Anthropic Messages API; credentials come from the environment
variable named in `api_key_env`, never from the options file.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_OPTIONS = {
    "provider": "openai",             # openai | azure | anthropic
    "model": "",                      # required
    "base_url": "https://api.openai.com/v1",
    "api_key_env": "OPENAI_API_KEY",
    "azure_api_version": "2025-04-01-preview",
    "temperature": None,              # omitted unless set (some reasoning models refuse it)
    "reasoning_effort": None,
    "max_tokens": 4096,
    "ingest_max_tokens": 16,
    "max_steps": 16,
    "context_budget": 2500,           # tokens of memory recalled up front
    "search_budget": 1200,            # tokens returned by each search_memory call
    "embedder": "arctic-m",           # arctic-m | minilm | none
    "rerank": "auto",
    "prices": None,                   # {"input": $/Mtok, "output": $/Mtok, "cached_input": $/Mtok}
    "store": None,                    # default: <work_dir>/commontrace
    "names": {"alex": "Alex Valdez", "morgan": "Morgan Chen", "riley": "Riley Tanaka"},
    "timeout": 300,
    "retries": 4,
}

INGEST_SYSTEM = ("You are {name}'s personal assistant. {name} is telling you about their day; it is "
                 "saved to your long-term memory automatically. Reply with a brief acknowledgement only.")

TEST_SYSTEM = """You are {name}'s personal assistant and act on their behalf through the app tools provided.
Today is {today}.

Your long-term memory is everything {name} has told you over the past years. It holds the people,
preferences, standing instructions, decisions, and dates you need. MEMORY below lists what was
recalled for this request; each entry is dated, and dates in [brackets] resolve relative time words.

Before acting, make sure every detail the request leaves implicit is grounded in memory: who exactly
(names, handles, addresses), which one, where, when, what wording, which tool or channel, and any
standing rule or preference that applies (days to avoid, people to include, formats, tone, places
{name} likes). If anything is missing or ambiguous, call search_memory with a short, specific query
(one person, project, place, or preference at a time) until it is grounded. When a fact changed over
time, the most recent statement wins unless the request asks about the past. Do not invent details
memory does not support, and do not ask {name} questions: complete the task with the app tools, then
reply with a one-line summary of what you did."""

SEARCH_TOOL = {
    "name": "search_memory",
    "description": ("Search your long-term memory of everything the user has told you. Use short, specific "
                    "queries: a person, project, place, preference or rule. Returns dated excerpts."),
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}


class HarnessError(RuntimeError):
    pass


# --- model clients ---------------------------------------------------------------------

def _post(url: str, headers: dict, body: dict, timeout: float, retries: int) -> dict:
    data = json.dumps(body).encode("utf-8")
    delay = 2.0
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - configured API URL
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code in (408, 409, 429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise HarnessError(f"model API {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise HarnessError(f"model API unreachable: {exc}") from None
    raise HarnessError("model API retries exhausted")


class ChatModel:
    """One chat call with tools -> (assistant message in the recorded shape, raw usage)."""

    def __init__(self, options: dict):
        self.o = options
        if not options.get("model"):
            raise HarnessError("options.model is required")

    def _key(self) -> str:
        key = os.environ.get(self.o["api_key_env"], "")
        if not key:
            raise HarnessError(f"set {self.o['api_key_env']} to the model API key")
        return key

    def settings(self, system: str, tools: list[dict], max_tokens: int) -> dict:
        out = {"model": self.o["model"], "system_prompt": system, "tools": tools, "max_tokens": max_tokens}
        if self.o.get("temperature") is not None:
            out["temperature"] = float(self.o["temperature"])
        if self.o.get("reasoning_effort"):
            out["reasoning_effort"] = str(self.o["reasoning_effort"])
        return out

    def complete(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> tuple[dict, dict]:
        if self.o["provider"] == "anthropic":
            return self._anthropic(system, messages, tools, max_tokens)
        return self._openai(system, messages, tools, max_tokens)

    def _openai(self, system, messages, tools, max_tokens):
        wire = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "assistant":
                entry = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    entry["tool_calls"] = [{"id": c["id"], "type": "function",
                                            "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                           for c in m["tool_calls"]]
                wire.append(entry)
            elif m["role"] == "tool":
                wire.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                wire.append({"role": "system" if m["role"] == "developer" else m["role"], "content": m["content"]})
        body = {"model": self.o["model"], "messages": wire, "max_completion_tokens": max_tokens}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        if self.o.get("temperature") is not None:
            body["temperature"] = float(self.o["temperature"])
        if self.o.get("reasoning_effort"):
            body["reasoning_effort"] = self.o["reasoning_effort"]
        if self.o["provider"] == "azure":
            url = (self.o["base_url"].rstrip("/") + "/chat/completions?api-version="
                   + self.o["azure_api_version"])
            headers = {"api-key": self._key()}
        else:
            url = self.o["base_url"].rstrip("/") + "/chat/completions"
            headers = {"Authorization": f"Bearer {self._key()}"}
        data = _post(url, headers, body, self.o["timeout"], self.o["retries"])
        choice = data["choices"][0]["message"]
        calls = []
        for c in choice.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {"_unparsed": c["function"].get("arguments")}
            calls.append({"id": c["id"], "name": c["function"]["name"],
                          "arguments": args if isinstance(args, dict) else {"value": args}})
        message = {"role": "assistant", "content": choice.get("content") or ""}
        if calls:
            message["tool_calls"] = calls
        return message, data.get("usage") or {}

    def _anthropic(self, system, messages, tools, max_tokens):
        wire: list[dict] = []
        system = "\n\n".join([system] + [m["content"] for m in messages if m["role"] == "developer"])
        for m in messages:
            if m["role"] == "developer":
                continue
            if m["role"] == "assistant":
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                           for c in m.get("tool_calls") or []]
                wire.append({"role": "assistant", "content": blocks})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if wire and wire[-1]["role"] == "user" and isinstance(wire[-1]["content"], list):
                    wire[-1]["content"].append(block)
                else:
                    wire.append({"role": "user", "content": [block]})
            else:
                wire.append({"role": "user", "content": m["content"]})
        body = {"model": self.o["model"], "system": system, "messages": wire, "max_tokens": max_tokens}
        if tools:
            body["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                              "input_schema": t["parameters"]} for t in tools]
        if self.o.get("temperature") is not None:
            body["temperature"] = float(self.o["temperature"])
        url = self.o.get("base_url") or "https://api.anthropic.com/v1"
        if "openai.com" in url:
            url = "https://api.anthropic.com/v1"
        data = _post(url.rstrip("/") + "/messages",
                     {"x-api-key": self._key(), "anthropic-version": "2023-06-01"},
                     body, self.o["timeout"], self.o["retries"])
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        calls = [{"id": b["id"], "name": b["name"], "arguments": b.get("input") or {}}
                 for b in data.get("content", []) if b.get("type") == "tool_use"]
        message = {"role": "assistant", "content": text}
        if calls:
            message["tool_calls"] = calls
        return message, data.get("usage") or {}


def _tool_text(result) -> str:
    """The visible text of an MCP tool result."""
    parts = []
    for item in getattr(result, "content", None) or []:
        parts.append(getattr(item, "text", None) or json.dumps(getattr(item, "model_dump", lambda: str(item))()))
    text = "\n".join(parts)
    if getattr(result, "isError", False):
        text = text or "error"
    return text


def _seal(db) -> None:
    """Fold the write-ahead log into the file and leave it self-contained, so the
    frozen file's hash is its whole content and it opens read-only anywhere."""
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("PRAGMA journal_mode=DELETE")


def _usage_cost(usage: dict, prices: dict) -> float:
    if "prompt_tokens" in usage:
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        fresh = int(usage.get("prompt_tokens") or 0) - cached
        output = int(usage.get("completion_tokens") or 0)
    else:
        cached = int(usage.get("cache_read_input_tokens") or 0)
        fresh = int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
        output = int(usage.get("output_tokens") or 0)
    return (fresh * float(prices["input"]) + cached * float(prices.get("cached_input", prices["input"]))
            + output * float(prices["output"])) / 1_000_000


# --- the harness -------------------------------------------------------------------------

class BenchmarkHarness:
    def __init__(self, options, work_dir):
        unknown = set(options or {}) - set(DEFAULT_OPTIONS)
        if unknown:
            raise ValueError(f"unknown options: {', '.join(sorted(unknown))}")
        self.options = {**DEFAULT_OPTIONS, **(options or {})}
        self.work_dir = str(work_dir)
        self.store_root = self.options["store"] or os.path.join(self.work_dir, "commontrace")

    # identity, local-only -------------------------------------------------------------
    def identity(self):
        import commontrace

        safe = {k: v for k, v in self.options.items() if k not in ("api_key_env",)}
        return {"harness": "commontrace", "commontrace_version": commontrace.__version__,
                "harness_sha256": hashlib.sha256(open(__file__, "rb").read()).hexdigest(),
                "options": safe, "memory": "commontrace conversation memory (SQLite, FTS5 BM25 + dense "
                                           "embeddings + cross-encoder rerank; read-only at test time)"}

    def _root(self, persona: str) -> str:
        return os.path.join(self.store_root, persona)

    def _name(self, persona: str) -> str:
        return (self.options.get("names") or {}).get(persona, persona.title())

    def _usage_log(self, phase: str) -> str:
        os.makedirs(self.store_root, exist_ok=True)
        return os.path.join(self.store_root, f"usage-{phase}.jsonl")

    def _record_usage(self, phase: str, persona: str, interaction: str, usage: dict) -> None:
        with open(self._usage_log(phase), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"persona": persona, "interaction": interaction, "usage": usage,
                                 "at": time.time()}) + "\n")

    # interactions ------------------------------------------------------------------------
    def run_interaction(self, request):
        if request.phase == "ingestion":
            return self._ingest(request)
        return asyncio.run(self._run_test(request))

    def _ingest(self, request):
        from harness.adapter import InteractionRecord

        from commontrace.conversation import Store

        day = str(request.narrative_time)[:10]
        with Store(self._root(request.persona), "history") as store:
            store.add(day, [{"id": request.interaction_id, "speaker": self._name(request.persona),
                             "role": "user", "text": request.message, "at": request.narrative_time}])
        model = ChatModel(self.options)
        system = INGEST_SYSTEM.format(name=self._name(request.persona))
        user = {"role": "user", "content": request.dated_message}
        started = time.time()
        reply, usage = model.complete(system, [user], [], self.options["ingest_max_tokens"])
        self._record_usage("ingestion", request.persona, request.interaction_id, usage)
        reply["usage"] = usage
        reply["duration_ms"] = round((time.time() - started) * 1000, 1)
        return InteractionRecord(model.settings(system, [], self.options["ingest_max_tokens"]),
                                 [{"role": "system", "content": system}, user, reply])

    def _recall(self, store, query: str, today: str, budget: int) -> str:
        from commontrace.conversation import Options, recall

        opts = Options(budget=budget, embedder=None if self.options["embedder"] == "none" else self.options["embedder"],
                       rerank=None if self.options["rerank"] == "none" else self.options["rerank"])
        return recall(store, query, now=today, options=opts).context or "(nothing relevant found)"

    async def _run_test(self, request):
        from examples.mcp_connection import connect_apps

        async with connect_apps(request.apps) as apps:
            listed = (await apps.list_tools()).tools
            return await self.run_agent(request, listed, apps.call_tool)

    async def run_agent(self, request, tools, call_app):
        from harness.adapter import InteractionRecord

        from commontrace.conversation import Store

        today = str(request.narrative_time)[:10]
        name = self._name(request.persona)
        app_tools = [{"name": t.name, "description": t.description or "",
                      "parameters": t.inputSchema or {"type": "object", "properties": {}}} for t in tools]
        all_tools = [SEARCH_TOOL] + app_tools
        store = Store(self._root(request.persona), "history", read_only=True)
        try:
            memory = self._recall(store, request.message, today, self.options["context_budget"])
            system = TEST_SYSTEM.format(name=name, today=today)
            recalled = {"role": "developer", "content": f"MEMORY (recalled for this request):\n{memory}"}
            user = {"role": "user", "content": request.dated_message}
            model = ChatModel(self.options)
            transcript = [recalled, user]
            started = time.time()
            for _step in range(int(self.options["max_steps"])):
                reply, usage = model.complete(system, transcript, all_tools, self.options["max_tokens"])
                self._record_usage("tests", request.persona, request.interaction_id, usage)
                reply["usage"] = usage
                transcript.append(reply)
                if not reply.get("tool_calls"):
                    break
                for call in reply["tool_calls"]:
                    if call["name"] == "search_memory":
                        text = self._recall(store, str(call["arguments"].get("query", "")), today,
                                            self.options["search_budget"])
                    else:
                        try:
                            text = _tool_text(await call_app(call["name"], call["arguments"]))
                        except Exception as exc:  # noqa: BLE001 - the model sees the failure, as a user would
                            text = f"tool error: {type(exc).__name__}: {exc}"
                    transcript.append({"role": "tool", "tool_call_id": call["id"], "content": text})
            if transcript[-1]["role"] != "assistant" or transcript[-1].get("tool_calls"):
                reply, usage = model.complete(system + "\nYou are out of steps: summarise what you did.",
                                              transcript, [], 512)
                self._record_usage("tests", request.persona, request.interaction_id, usage)
                reply["usage"] = usage
                transcript.append(reply)
            settings = model.settings(system, all_tools, self.options["max_tokens"])
            return InteractionRecord(settings, [{"role": "system", "content": system}] + transcript,
                                     duration_ms=round((time.time() - started) * 1000, 1))
        finally:
            store.close()

    # memory lifecycle -------------------------------------------------------------------
    def _files(self, persona: str) -> list[str]:
        from commontrace.conversation.store import conversations_dir

        directory = conversations_dir(self._root(persona))
        return sorted(os.path.join(directory, f) for f in os.listdir(directory) if f.endswith(".db"))

    def freeze(self, persona):
        from commontrace.conversation import Store
        from commontrace.conversation import embed as embed_mod
        from commontrace.conversation.search import _embedder

        with Store(self._root(persona), "history") as store:
            if self.options["embedder"] != "none":
                embedder = _embedder(store, self.options["embedder"])
                if embedder is None:
                    raise HarnessError("the embedder is not installed (pip install 'commontrace[attention]')")
                embed_mod.search(store, embedder, embedder.encode(["warm"], query=True)[0], 1)
                _seal(embedder.db)
                embedder.db.close()
            stats = store.stats()
            _seal(store.db)
        digests = {os.path.basename(p): hashlib.sha256(open(p, "rb").read()).hexdigest() for p in self._files(persona)}
        return {"persona": persona, "files": digests, "turns": stats.get("turns"), "units": stats.get("units")}

    def verify_checkpoint(self, persona, checkpoint):
        current = {os.path.basename(p): hashlib.sha256(open(p, "rb").read()).hexdigest()
                   for p in self._files(persona)}
        if current != checkpoint.get("files"):
            raise HarnessError(f"{persona}: stored memory differs from its checkpoint")

    def total_cost_usd(self, phase):
        prices = self.options.get("prices")
        if not prices or "input" not in prices or "output" not in prices:
            raise HarnessError("set options.prices {input, output, cached_input} in USD per million tokens")
        path = self._usage_log(phase)
        if not os.path.exists(path):
            raise HarnessError(f"no usage records for {phase}")
        total = 0.0
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    total += _usage_cost(json.loads(line)["usage"], prices)
        return round(total, 6)
