# @commontrace/hub-client

A thin TypeScript client for a [CommonTrace Hub](../../hub/README.md)'s
MCP tool surface — audit §6.4 ("No mobile / JVM / .NET SDKs... a
TypeScript client is the first one worth building").

Any MCP-capable client can already reach the whole tool surface without
an SDK, so this is a **convenience layer**, not a new capability: typed
request/response shapes for the tools most integrations reach for
first, and one generic `call()` for everything else — including any
tool a deployment adds after this package is published.

Deliberately thinner than the Python client
([`commontrace/hub_client.py`](../../commontrace/hub_client.py)), which
also drives `commontrace sync`'s bulk local↔Hub reconciliation
(idempotency-key bookkeeping, adaptive pacing across hundreds of files,
a five-way error taxonomy for a batch CLI's exit code). None of that is
part of being an MCP client for a Hub; a project that needs bulk sync
still wants the Python CLI for that specific job.

## Install

```bash
npm install @commontrace/hub-client
```

## Use

```ts
import { HubClient } from "@commontrace/hub-client";

const client = await HubClient.connect(
  "https://your-hub.example.com/mcp",
  process.env.COMMONTRACE_HUB_API_KEY!,
);

const { traces } = await client.searchTraces({ query: "how do I retry a flaky webhook" });

await client.contributeTrace({
  title: "Retry webhook delivery on 5xx",
  context_text: "A partner's endpoint returned 502 intermittently.",
  solution_text: "Exponential backoff, 8 attempts, give up loudly on the last one.",
  agent_type: "code",
});

// Any tool this client has no dedicated method for yet is still reachable:
await client.call("assign_trace", { trace_id: "…", user_id: "…" });

// Done: release the connection, or a Node process may not exit.
await client.close();
```

`connect()` sends the API key on every call, so it refuses a URL it will
not send it to, before connecting: anything but `http`/`https`, plaintext
`http://` to anything but localhost, and link-local or cloud-metadata
addresses. The same rules as the Python client; `checkHubUrl(url)` applies
them on their own.

## Framework memory helpers

The package also exports `MemoryClient` for the gateway's scoped one-call
`turn` API, plus optional structural adapters for Vercel AI SDK and Mastra. The
framework packages are caller-provided optional dependencies: this package
does not import or install either framework.

`MemoryClient` also exposes versioned record put/search/forget calls and
`reapDueRecords(limit = 16)` for bounded retention cleanup. The reaper accepts
limits from 1 through 100 and requires a Hub API key with the `admin` scope.

Vercel AI SDK middleware uses the current V4 language-model middleware
contract. It retrieves once when a new user turn begins, keeps the memory block
through tool-call continuations, and removes the previous tagged block before
the next user turn. Final text is recorded through the SessionEnd event; only
`stop` finishes are marked successful, while other finish reasons are recorded
as unsuccessful when text is available. Retrieval
requests have a 40 ms timeout and fail open. Completion capture has a 250 ms
request timeout and cannot replace or delay a generated response.

```ts
import {wrapLanguageModel} from "ai";
import {MemoryClient, vercelMemoryMiddleware} from "@commontrace/hub-client";

const memory = new MemoryClient({
  url: "http://127.0.0.1:8787",
  agentId: "support-agent",
  context: ["user:alice"],
});
const modelWithMemory = wrapLanguageModel({
  model,
  middleware: vercelMemoryMiddleware(memory, "conversation-42"),
});
```

Mastra uses its `MessageList` input processor contract and a separate output
processor for final-response capture. It replaces only the system message tagged
`commontrace-memory`. Pass `getSessionId` and `getContext` when one processor and
client serve multiple request threads or users; a missing session or configured
principal scope skips memory access rather than falling back to an unscoped
request. Output capture runs in the background and does not hold up the result.

```ts
import {mastraMemoryOutputProcessor, mastraMemoryProcessor} from "@commontrace/hub-client";

const inputProcessor = mastraMemoryProcessor(memory, "fallback-thread", {
  getSessionId: requestContext => {
    const threadId = (requestContext as {get(key: string): unknown}).get("mastra__threadId");
    return typeof threadId === "string" ? threadId : undefined;
  },
  getContext: requestContext => {
    const userId = (requestContext as {get(key: string): unknown}).get("userId");
    return typeof userId === "string" ? [`user:${userId}`] : undefined;
  },
});
const outputProcessor = mastraMemoryOutputProcessor(memory, "fallback-thread", {
  getSessionId: requestContext => {
    const threadId = (requestContext as {get(key: string): unknown}).get("mastra__threadId");
    return typeof threadId === "string" ? threadId : undefined;
  },
  getContext: requestContext => {
    const userId = (requestContext as {get(key: string): unknown}).get("userId");
    return typeof userId === "string" ? [`user:${userId}`] : undefined;
  },
});
// Add these to the Mastra Agent's inputProcessors and outputProcessors arrays.
```

These adapters are fixture-tested and structurally type-checked against the
current framework contracts. An isolated smoke against `ai@7.0.137` exercised
real `generateText` and `streamText` calls; a smoke against `@mastra/core@1.76.0`
used its real `MessageList` API. A full provider-free Mastra `Agent.generate()`
smoke also passed with `MockLanguageModelV4` and a fake local gateway; its test
harness increased the input deadline to 1,000 ms to account for instrumentation.
The 40 ms timeout is a per-request bound, not a measured end-to-end hook p95.

## Error handling

- `HubToolError` — the Hub ran the tool and it said no (`forbidden`,
  `not_found`, `person_required`, …). `err.code` is the machine-readable
  reason; `err.body` is the full payload. Retrying verbatim will fail
  identically, **except** `rate_limited`, which `call()` already retries
  for you (see below) — you will only ever see a `rate_limited`
  `HubToolError` once the retry budget is spent.
- `HubConfigurationError` — `connect()` refused the URL (see above); the
  configuration has to change.
- `HubConnectionError` — the request never reached a tool-level answer:
  transport failure, unreachable server, a malformed response.

## Rate limiting

`call()` retries a `rate_limited` tool-level refusal (a write-limiter
hit, returned inside a normal 200 response — see `hub/server.py`), up to
`maxAttempts` (default 3), waiting the server's own `retry_after` when
it sends one:

```ts
const client = await HubClient.connect(url, apiKey, { maxAttempts: 5 });
```

Any other tool-level error is raised immediately — a permission denial
does not become retryable no matter how many attempts remain.

## Testing your own code against this client

`HubClient.withCaller(fakeCaller)` builds a client around any object
implementing `{ callTool(name, args) }`, with no network and no live
Hub required — the exact seam this package's own tests use (see
`test/client.test.ts`).

## Scope

Typed convenience methods exist today for `search_traces`,
`contribute_trace`, `get_trace`, `vote_trace`, `amend_trace`,
`list_tags`, and `account_usage`. Every other Hub tool (currently 26 in
total) is reachable through `client.call(name, args)`.
