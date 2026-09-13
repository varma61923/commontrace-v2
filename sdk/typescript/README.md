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
```

## Error handling

- `HubToolError` — the Hub ran the tool and it said no (`forbidden`,
  `not_found`, `person_required`, …). `err.code` is the machine-readable
  reason; `err.body` is the full payload. Retrying verbatim will fail
  identically, **except** `rate_limited`, which `call()` already retries
  for you (see below) — you will only ever see a `rate_limited`
  `HubToolError` once the retry budget is spent.
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
