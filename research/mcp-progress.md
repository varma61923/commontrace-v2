# Opt-in native MCP progress

Local conversation tools now report actual operation phases through the MCP SDK's `Context.report_progress` API. This implements the progress-notification portion of the supplied roadmap without adding a custom streaming protocol or changing final tool results.

Five existing tools support progress:

| Tool | Start notification | Successful completion |
| --- | --- | --- |
| `conversation_add` | Storing conversation messages | Conversation messages stored |
| `conversation_search` | Searching conversation memory | Conversation search complete |
| `conversation_recall` | Retrieving conversation evidence | Conversation retrieval complete |
| `memory_recall` | Retrieving memory evidence | Memory retrieval complete |
| `conversation_summarize` | Summarizing conversation sessions | Conversation summaries complete |

Each notification uses `notifications/progress` with the client's requested `progressToken`, progress `0` before work starts, progress `1` after successful work, and total `1`. These are operation-phase values rather than an estimate of elapsed time or a fabricated percentage. A failed operation keeps its existing error result and does not emit successful completion.

Notifications are enabled only when the request metadata carries a valid string or integer token in `_meta.progressToken`; integer zero and empty strings are valid. Clients that do not request progress continue receiving only the final response. There are no additional tool names or visible tool arguments: SDK request context is injected and excluded from the input JSON schema.

The SDK handles JSON-RPC framing, request-token correlation and serialization of concurrent notifications. CommonTrace does not write notification JSON to stdout itself. The compatibility adapter now forwards request context to SDK versions whose `call_tool` accepts it; versions that resolve context internally retain the prior call signature. Minimal adapters that export the server class without Context retain the same tools and hidden argument schemas, with optional progress disabled. Context types are resolved when building the optional MCP server, so importing the ordinary local Python package does not acquire a new MCP dependency.

Progress carries only fixed operation labels, numeric phase values and the required correlation token. It contains no messages, question text, facts, retrieved contexts, space/session identifiers, model output or exception details. Optional notification delivery failures do not change a successful storage operation's result. Request cancellation still propagates.

Conversation ingestion and extractive summarization now run in owned worker threads, matching the existing conversation retrieval tools. Each worker creates and closes its own Store connection. This keeps the MCP event loop responsive while SQLite or summarization runs. Progress is sent from the event loop before and after awaited work, never directly from a worker thread. The CLI-backed distillation path still captures process stdout; it is deliberately not moved into an unsafe concurrent stdout-redirection worker.

No ingestion/extraction/index callback API was added to Store or the embedding engine. Batch-proportional progress could be added later where a cancellable, bounded callback contract is available. Token chunks, inferred progress, new model calls and nonstandard stream messages are not implemented.

## Verification

```bash
python -m pytest tests/test_mcp_progress.py tests/test_mcp_server.py tests/test_mcp_cognitive_tools.py tests/test_mcp_fusion.py -q
python -m ruff check commontrace/mcp_server.py tests/test_mcp_progress.py
```

The new tests cover opt-in token validation, numeric zero, hidden context schemas, compatibility with the older call signature and Context-absent adapters, forwarding a real SDK Context, failed-operation completion behavior, harmless notification delivery failures, worker/event-loop responsiveness and cancellation. A real subprocess stdio test drives the SDK client: a default ingestion emits no progress, and two concurrent opted-in ingestions emit four readable notifications with separate request tokens and independently ordered start/completion callbacks. The final tool responses remain ordinary successful results, and the client continues using the same connection afterwards. The test also checks that private message content and source identifiers do not appear in progress frames.
