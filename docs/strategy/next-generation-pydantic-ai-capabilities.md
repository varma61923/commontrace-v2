# Pydantic AI memory capabilities

The existing `commontrace.integrations.pydantic_ai` module had a recall tool and outcome recorder, while the shared middleware exposed only a legacy history callback. `memory_capabilities(memory)` now wires the current Pydantic AI `ProcessHistory` and `Hooks(after_run=...)` APIs. It performs a scoped `MemoryClient.turn` on the first model request of each run, caches that context or failed attempt by `RunContext.run_id`, removes a prior CommonTrace context block before reprocessing history, and injects the current block as a clearly marked system prompt part. The after-run hook records the successful output through the existing `SessionEnd` path and returns the exact result object. Retrieval and capture waits default to 40 ms and 250 ms; callers can set `retrieval_timeout` and `capture_timeout`. Read or write failures are swallowed so the agent keeps its original history/result, and a failed read is not retried during the same run.

```python
from pydantic_ai import Agent

from commontrace.client import MemoryClient
from commontrace.integrations.pydantic_ai import memory_capabilities

memory = MemoryClient(
    ".commontrace",
    agent_id="support",
    context={"tenant": "acme", "user": "alice"},
)
agent = Agent("openai:gpt-5.2", capabilities=memory_capabilities(memory))
result = await agent.run("What response style do I prefer?", conversation_id="chat-42")
```

Keep a `MemoryClient` scoped to the authenticated tenant/user. The default `occasion="conversation_id"` uses the conversation identifier available on the run or current messages, falling back to `run_id` when none is available. Use `occasion="run_id"` when each run should be isolated. The user message is captured and retrieved in one `turn` operation; the final assistant result is recorded after successful completion. The SDK is optional and imported only when `memory_capabilities` is constructed. The timeouts bound how long hooks wait; because the existing `MemoryClient` calls are synchronous, a timed-out worker may finish in the background and cannot be forcibly cancelled.

The official API contract is documented in [Process History](https://pydantic.dev/docs/ai/capabilities/process-history/) and [Pydantic AI capability hooks](https://ai.pydantic.dev/capabilities/custom/). The implementation follows the scoped run-cache and system-context injection pattern in [Zep's Pydantic AI history processor](https://github.com/getzep/zep/tree/main/integrations/pydantic-ai/python/src/zep_pydantic_ai), while using CommonTrace's existing turn and session-end operations rather than adding a storage layer.

A credential-free optional-SDK run with `pydantic-ai==2.55.0` and its
official `TestModel` exercised a real `Agent.run()`: the scoped memory context
was injected once, the final output was captured, and the run result was
returned unchanged. Together with the OpenAI Agents `ScriptedModel` run, the
installed-SDK adapter suite passed 21 tests; the base environment passed 13 and
skipped 8 optional tests. These tests do not exercise provider transport or
credentials.
