import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { FetchLike } from "@modelcontextprotocol/sdk/shared/transport.js";

import { HubConnectionError, HubToolError } from "./errors.js";
import type {
  AccountUsage,
  AmendTraceArgs,
  ContributeTraceArgs,
  SearchTracesArgs,
  SearchTracesResult,
  ToolErrorBody,
  Trace,
} from "./types.js";

/** The seam tests inject a fake into: anything that can call a Hub tool
 * by name and return its JSON body. `HubClient.connect` builds a real
 * one over the official MCP SDK; `HubClient.withCaller` accepts any
 * other implementation, no network or live server required. */
export interface ToolCaller {
  callTool(name: string, args: Record<string, unknown>): Promise<Record<string, unknown>>;
}

export interface HubClientOptions {
  /** How many times `call()` will retry a `rate_limited` tool-level
   * refusal before giving up and raising `HubToolError`. Default 3,
   * matching commontrace/hub_client.py's own default attempt budget for
   * the same failure mode (see `call`'s docstring below). */
  maxAttempts?: number;
  /** Override fetch (e.g. for a proxy, or a mock in tests). Passed
   * straight through to the MCP SDK's StreamableHTTPClientTransport. */
  fetch?: FetchLike;
}

function isErrorBody(body: Record<string, unknown>): body is ToolErrorBody {
  return typeof body.error === "string";
}

/**
 * A thin, typed client for one CommonTrace Hub deployment's MCP tool
 * surface (hub/server.py) -- audit §6.4: "Python only, plus MCP... a
 * TypeScript client is the first [non-Python SDK] worth building."
 *
 * "Any MCP-capable client already reaches the whole tool surface
 * without an SDK" was already true before this existed, so this is a
 * convenience layer, not a new capability: typed request/response
 * shapes for the tools an integrator reaches for first, one generic
 * `call()` for everything else (including any tool added after this
 * package was published), and the one retry behaviour
 * commontrace/hub_client.py's own module history says is worth having
 * by default (see `call`).
 *
 * Deliberately thin compared to hub_client.py: that module also drives
 * `commontrace sync`'s bulk local<->Hub reconciliation (idempotency-key
 * bookkeeping, adaptive pacing across hundreds of files, a five-way
 * error taxonomy for a batch CLI's exit code). None of that is part of
 * being an MCP client for a Hub; a caller that needs bulk sync still
 * wants the Python CLI for that specific job.
 */
export class HubClient {
  private readonly caller: ToolCaller;
  private readonly maxAttempts: number;

  private constructor(caller: ToolCaller, maxAttempts: number) {
    this.caller = caller;
    this.maxAttempts = Math.max(1, maxAttempts);
  }

  /**
   * Connect to a Hub over its streamable-HTTP MCP endpoint (`/mcp` by
   * default -- see `HUB_STREAMABLE_HTTP_PATH`), authenticating with an
   * API key from `python -m hub.manage issue-key`.
   */
  static async connect(url: string, apiKey: string, options: HubClientOptions = {}): Promise<HubClient> {
    const mcpClient = new Client({ name: "commontrace-hub-client-ts", version: "0.1.0" });
    const transport = new StreamableHTTPClientTransport(new URL(url), {
      requestInit: { headers: { Authorization: `Bearer ${apiKey}` } },
      fetch: options.fetch,
    });
    try {
      await mcpClient.connect(transport);
    } catch (err) {
      throw new HubConnectionError(`could not connect to the Hub at ${url}`, err);
    }
    const caller: ToolCaller = {
      async callTool(name, args) {
        let result;
        try {
          result = await mcpClient.callTool({ name, arguments: args });
        } catch (err) {
          throw new HubConnectionError(`${name}: request failed`, err);
        }
        if (result.structuredContent && typeof result.structuredContent === "object") {
          return result.structuredContent as Record<string, unknown>;
        }
        const first = Array.isArray(result.content) ? result.content[0] : undefined;
        const text = first && typeof first === "object" && "text" in first ? (first as { text: string }).text : "{}";
        try {
          return JSON.parse(text) as Record<string, unknown>;
        } catch (err) {
          throw new HubConnectionError(`${name}: response was not valid JSON`, err);
        }
      },
    };
    return new HubClient(caller, options.maxAttempts ?? 3);
  }

  /** Wrap an already-built caller directly -- how tests exercise this
   * class's retry/error-mapping logic without a live server. */
  static withCaller(caller: ToolCaller, options: HubClientOptions = {}): HubClient {
    return new HubClient(caller, options.maxAttempts ?? 3);
  }

  /**
   * Call ANY Hub tool by name. Every typed method below is a thin
   * wrapper over this one -- a tool this client has no dedicated method
   * for yet (including one added to a deployment after this package was
   * published) is still reachable through it.
   *
   * Retries ONLY a `rate_limited` tool-level refusal (hub/server.py
   * returns this INSIDE a 200 response for a write-limiter refusal, not
   * as an HTTP-level 429), waiting `retry_after` seconds if the server
   * sent one. commontrace/hub_client.py's own module docstring
   * documents why this default exists: without it, a bulk write that
   * hits a shared write limiter reads every "come back in two seconds"
   * as a permanent per-call failure -- measured there at losing most of
   * a batch to a limiter that was only ever asking for patience. Any
   * OTHER tool-level error (`forbidden`, `not_found`, `person_required`,
   * ...) is raised immediately as `HubToolError`: retrying a permission
   * denial does not make it succeed.
   */
  async call<T = Record<string, unknown>>(name: string, args: Record<string, unknown> = {}): Promise<T> {
    for (let attempt = 1; ; attempt += 1) {
      const body = await this.caller.callTool(name, args);
      if (isErrorBody(body)) {
        if (body.error === "rate_limited" && attempt < this.maxAttempts) {
          const retryAfterSeconds = typeof body.retry_after === "number" ? body.retry_after : 1;
          await new Promise((resolve) => setTimeout(resolve, retryAfterSeconds * 1000));
          continue;
        }
        throw new HubToolError(name, body);
      }
      return body as T;
    }
  }

  // --- Typed convenience wrappers for the surface most integrations reach for first ---

  searchTraces(args: SearchTracesArgs = {}): Promise<SearchTracesResult> {
    return this.call<SearchTracesResult>("search_traces", args as Record<string, unknown>);
  }

  contributeTrace(args: ContributeTraceArgs): Promise<Trace> {
    return this.call<Trace>("contribute_trace", args as unknown as Record<string, unknown>);
  }

  getTrace(id: string): Promise<Trace> {
    return this.call<Trace>("get_trace", { id });
  }

  voteTrace(
    id: string, vote: "up" | "down", feedbackTag = "", feedbackText = "",
  ): Promise<Trace> {
    return this.call<Trace>("vote_trace", {
      id, vote, feedback_tag: feedbackTag, feedback_text: feedbackText,
    });
  }

  amendTrace(args: AmendTraceArgs): Promise<Trace> {
    return this.call<Trace>("amend_trace", args as unknown as Record<string, unknown>);
  }

  listTags(): Promise<{ tags: string[] }> {
    return this.call<{ tags: string[] }>("list_tags", {});
  }

  accountUsage(): Promise<AccountUsage> {
    return this.call<AccountUsage>("account_usage", {});
  }
}
