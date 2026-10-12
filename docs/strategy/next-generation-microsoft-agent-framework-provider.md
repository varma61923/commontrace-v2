# Microsoft Agent Framework context provider

`commontrace.integrations.microsoft_agent_framework.CommonTraceContextProvider`
adapts the Microsoft Python Agent Framework's current context-provider hooks.
Construct it with a `MemoryClient` and add it to an agent's
`context_providers` list:

```python
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from commontrace.client import MemoryClient
from commontrace.integrations.microsoft_agent_framework import CommonTraceContextProvider

memory = MemoryClient(
    "./memory",
    agent_id="support-agent",
    context={"tenant": "acme", "user": "alice"},
)
provider = CommonTraceContextProvider(memory)
agent = Agent(
    client=OpenAIChatClient(model="gpt-4o-mini"),
    context_providers=[provider],
)
session = agent.create_session()
response = await agent.run("What is the release verification procedure?", session=session)
```

The adapter reads the latest user message in `SessionContext.input_messages`.
Its async `before_run` hook uses one existing `Middleware.before_run` call,
which delegates to one `MemoryClient.turn` operation to capture that message
and retrieve relevant context. Retrieved text is added through
`SessionContext.extend_instructions`. Local and remote storage therefore use
the same CommonTrace path. Synchronous storage work runs in a worker thread so
the async Agent Framework loop can continue while I/O or local search runs.

The user and tenant dimensions come from the provided `MemoryClient`, or the
caller may pass `user_id=` when constructing the provider. A shared provider
with no configured user can resolve `context.options["user_id"]` per run. A
missing user or session ID raises explicitly rather than making an unscoped
request. The Agent Framework session ID is passed as the CommonTrace turn ID;
the adapter does not add a session dimension to the memory scope, so user
facts remain searchable across sessions. Existing scopes on the client are
preserved, and the user dimension is always explicit.

The async `after_run` hook records assistant text through the existing
`Middleware.after_run` / `SessionEnd` path, bounded to 20,000 characters. It
does not alter or replace the framework response. Provider-scoped state tracks
whether the paired user turn was captured, and a matching session/query check
prevents an unrelated response from being recorded. The provider opts into
`after_run_once_per_turn`, so Microsoft Agent Framework defers capture until
an agent loop completes. Retrieval and capture failures are debug-logged and
fail open; missing identity remains a configuration error.

This adapts the lifecycle and scoping lessons in Zep's
`integrations/ms-agent-framework/python/src/zep_ms_agent_framework/context_provider.py`
and Supermemory's
`packages/agent-framework-python/src/supermemory_agent_framework/context_provider.py`.
Both use `before_run` for context injection and `after_run` for response
capture. The Microsoft-owned [Mem0 provider](https://github.com/microsoft/agent-framework/blob/main/python/packages/mem0/agent_framework_mem0/_context_provider.py)
also treats retrieval scope as an explicit boundary instead of implicitly
falling back to a shared agent scope.
CommonTrace keeps its own storage and scope contract: it reuses the existing
turn and SessionEnd operations rather than adding an SDK-specific memory
client or store.

The current API contract was checked against Microsoft's maintained
[`ContextProvider` source](https://github.com/microsoft/agent-framework/blob/main/python/packages/core/agent_framework/_sessions.py),
the [`Agent` provider execution code](https://github.com/microsoft/agent-framework/blob/main/python/packages/core/agent_framework/_agents.py),
and the [core package installation instructions](https://github.com/microsoft/agent-framework/blob/main/python/packages/core/README.md).
Those sources define `source_id`, async keyword-only `before_run` and
`after_run`, provider-scoped state, and `after_run_once_per_turn`. The adapter
imports `agent_framework` only when its provider is constructed; the core
CommonTrace package and this adapter module remain importable without the
optional `agent-framework-core` distribution.

Focused tests use a fixture that models the current hook shape plus a real
temporary CommonTrace journal. They verify exactly one `MemoryClient.turn`
per user run, Alice/Bob retrieval and capture isolation, fail-open outages,
output preservation, missing-scope rejection, and import safety without the
optional SDK. No live model or installed Agent Framework distribution is
required for these contract tests.
