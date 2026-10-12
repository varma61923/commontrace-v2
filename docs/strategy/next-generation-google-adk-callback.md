# Google ADK before-model callback

`commontrace.integrations.google_adk.before_model_callback(memory)` returns a
synchronous callback for `LlmAgent(before_model_callback=...)`. At invocation,
it reads the ADK app, user and session identifiers, builds a `MemoryClient`
scoped to that identity, and delegates to
`Middleware.process_llm_request`. That path makes one `MemoryClient.turn`
request to capture the latest user text and retrieve context, appends retrieved
context as a user `google.genai.types.Content`, and returns `None` so ADK runs
the model.

Google packages are not imported when the integration module is imported or
the callback factory is created. `google.genai.types` is imported only when a
nonempty context block must be appended. A missing session ID or user ID (and
no configured user scope) raises `ValueError` rather than using an unscoped
memory lane. Storage and retrieval errors are debug-logged and return `None`,
letting ADK continue without injected memory.

The adapter follows Google's documented synchronous
`before_model_callback(callback_context, llm_request)` lifecycle and `None`
continuation contract (official source:
`https://github.com/google/adk-python/blob/main/.agents/skills/adk-agent-builder/references/callbacks-and-plugins.md#the-eight-agent-callbacks`);
see
`/root/Test/zep/integrations/adk/typescript/src/before-model-callback.ts:62-82,110-128`
for Zep's per-turn callback and one-round-trip persist/retrieve pattern, and
`/root/Test/zep/integrations/adk/go/seam_test.go:120-148` for its assertion
that one call persists one message and injects returned context. The Mem0
reference at
`/root/Test/mem0/docs/integrations/google-ai-adk.mdx:40-104,108-128` maps
ADK's `BaseMemoryService` search on both `user_id` and `app_name` and persists
completed session events with explicit role mapping. CommonTrace adopts its
identity scoping, while using the framework middleware's single turn API
instead of a separate ADK memory service.

The focused tests use fake `google.genai.types` classes and a real temporary
CommonTrace journal. They check exactly one `MemoryClient.turn` call, stored
user text, retrieval and injection of an Alice-scoped record, exclusion of
that record for Bob, and fail-open behavior when the memory store raises. A
separate import test blocks all Google imports and checks that importing this
adapter creates no memory store.

This is a component contract test, not a live ADK run. It covers textual user
parts only. It does not implement ADK `BaseMemoryService`, save completed
assistant responses, or deduplicate repeated before-model callback invocations
within an ADK tool loop. The callback's `MemoryClient` store is supplied by the
caller, and external stores still need their normal lifecycle/provisioning.
