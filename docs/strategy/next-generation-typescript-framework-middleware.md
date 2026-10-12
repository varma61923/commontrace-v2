# TypeScript Vercel AI SDK and Mastra memory middleware

This slice closes the TypeScript framework-hook portion of F2 for the two
frameworks already represented by tool factories in `sdk/typescript`. It keeps
the existing `MemoryClient.turn` gateway API and adds small structural adapters,
so applications install their own Vercel AI SDK or Mastra version without
pulling either dependency into the CommonTrace client.

## Behavior

The Vercel adapter implements the AI SDK V4 language-model middleware shape.
For a new user turn it removes prior CommonTrace-tagged user blocks, performs a
single scoped `turn` request with a 40 ms request deadline, and appends returned
context as a `<memory_update>` block. Tool-loop calls whose latest prompt item
is an assistant or tool message preserve the injected block and do not repeat
retrieval. `wrapGenerate` and `wrapStream` record final text through the
SessionEnd event; only `stop` finishes are marked successful, while other
finish reasons are marked unsuccessful. The stream
wrapper forwards each part unchanged. Completion persistence is best-effort and
capped at 20,000 characters, with a 250 ms request deadline.

The Mastra input processor uses the current `MessageList` methods to remove the
previous `commontrace-memory` system message and add the next tagged block. It
can resolve the session/thread for each call through `getSessionId` and
additional principal labels through `getContext`, which supports a shared
processor and client across requests. If a configured scope resolver produces
no scope, it skips memory access. A separate output processor records final
assistant text asynchronously and returns the original messages without
waiting on storage.

Both adapters fail open on memory service errors. The 40 ms retrieval timeout
is a per-request cap; it is not evidence that complete host startup or hook
latency is below the F2 p95 target.

## Competitor patterns and compatibility

The implementation adapts the Zep Vercel middleware pattern of retrieving on
new user turns, avoiding repeated tool-loop reads, and capturing only the final
answer (`/root/Test/zep/integrations/vercel-ai/typescript/src/middleware.ts`).
For Mastra it follows the request-context session resolution and tagged system
message replacement used by Supermemory
(`/root/Test/supermemory/packages/tools/src/mastra/processor.ts`). No competitor
source was copied into this slice.

The middleware contract was checked against the current official
[AI SDK middleware reference](https://ai-sdk.dev/docs/ai-sdk-core/middleware)
and its published type surface; the processor shape was checked against
[Mastra's memory input processor reference](https://mastra.ai/reference/processors/memory-input-filter)
and the current `Processor` type. A separate isolated runtime smoke used
`ai@7.0.137` and `@mastra/core@1.76.0` with Node 22.23.3: the real AI SDK's
`generateText` and `streamText` both ran through `wrapLanguageModel`, retrieved
memory, and returned their model text. A provider-free full Mastra
`Agent.generate()` smoke also passed on the same Mastra version with
`MockLanguageModelV4`: the input processor injected context visible to the mock
model, and the output processor delivered its `SessionEnd` capture to the fake
gateway. Its temporary harness raised the 40 ms retrieval deadline to 1,000 ms
to remove test instrumentation overhead; the production deadline was not
measured under the Agent runtime. None of these checks used provider credentials
or measured host hook latency.

## Verification

`sdk/typescript/test/vercel.test.ts` covers user-turn deduplication, stale
context replacement, missing-session cleanup, fail-open retrieval, final-only
capture, finish-state reporting, stream passthrough, and capture failure.
`sdk/typescript/test/mastra.test.ts` covers tagged `MessageList` updates,
request-scoped session and principal IDs, missing-scope skip, stale-context
cleanup, fail-open retrieval, output capture, and detached persistence.
`sdk/typescript/test/memory.test.ts` also checks that request deadlines are not
serialized into gateway payloads.

A temporary fixture passed the adapter into the installed AI SDK's actual
`wrapLanguageModel()` middleware parameter and assigned both Mastra processors
to its `Processor` type; the build passed. The runtime smoke above exercised the
Vercel SDK's real generate and stream paths and Mastra's real `MessageList` API.
Neither package is declared as a CommonTrace dependency. After an
exact-lockfile `npm ci`, `npm test` passed all 37 SDK tests with no framework
packages installed. The full Mastra Agent smoke is documented above; the broad
host hook p95 target remains unmeasured.
