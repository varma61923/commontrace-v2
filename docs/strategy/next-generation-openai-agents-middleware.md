# OpenAI Agents SDK middleware adapter

The existing `memory_tool()` remains useful when an agent should choose when to recall. The gap was automatic per-user-turn injection: `Middleware.openai_call_model_input_filter()` invokes `MemoryClient.turn()` on every model request in a tool loop and did not capture the completed assistant output.

`OpenAIAgentsMemory` uses the SDK's `RunConfig.call_model_input_filter` to inspect the prepared Responses input and add memory to model instructions. One adapter instance is scoped to one `Runner.run()` invocation, so repeated filter calls reuse the first user-turn result across tool calls and handoffs. It pins the run's session ID and user scope without mutating a shared `MemoryClient`; a missing session or user scope and any retrieval error leave model input unchanged. Its lazily constructed `RunHooks` captures the final agent output with a `SessionEnd` event, bounded by a configurable 40 ms default so slow storage does not hold the completed response.

```python
adapter = OpenAIAgentsMemory(memory, session_id=session_id, user_id=user_id)
result = await Runner.run(
    agent,
    prompt,
    context=run_context,
    hooks=adapter.run_hooks(),
    run_config=RunConfig(call_model_input_filter=adapter.call_model_input_filter),
)
```

The callback contract follows the current SDK references for [`call_model_input_filter`](https://openai.github.io/openai-agents-python/running_agents/#call-model-input-filter) and [`RunHooks.on_agent_end`](https://openai.github.io/openai-agents-python/ref/lifecycle/). The implementation pattern borrows the competitors' split between automatic prompt injection and final conversation capture: Hindsight exposes dynamic `memory_instructions()` plus tools in `hindsight-integrations/openai-agents/hindsight_openai_agents/tools.py`, while Supermemory and Mem0 document explicit pre-run recall and post-run storage in `apps/docs/integrations/openai-agents-sdk.mdx` and `docs/integrations/openai-agents-sdk.mdx`. CommonTrace applies that pattern through the SDK's native input filter and lifecycle hooks rather than adding a mandatory package dependency.

The credential-free optional-SDK suite also runs a real `Runner.run()` with
`openai-agents==0.23.1` and the official `ScriptedModel`. It verified one scoped
injection, final `SessionEnd` capture, and unchanged result delivery. This
exercised SDK orchestration without provider credentials or network calls. That
run exposed and fixed `memory_tool()`'s local `RunContextWrapper` annotation:
with postponed annotations, the SDK's `function_tool` could not resolve the
local type name. The fix installs the already-imported type directly into the
function annotations. The combined installed-SDK adapter run passed 21 tests;
the base environment passed 13 and skipped 8 optional tests. Real provider
transport remains untested.
