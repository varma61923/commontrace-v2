import type { ToolErrorBody } from "./types.js";

/** The Hub reached the request, ran the tool, and the tool itself said no
 * -- a scope/capability denial, a not-found, a schema violation, rate
 * limiting, or any other `{"error": ...}` payload (hub/server.py's
 * `_error_response`). Distinct from `HubConnectionError`: the request
 * DID complete, and retrying it verbatim will fail identically, except
 * for `rate_limited`, which `HubClient.call` already retries on your
 * behalf (see its docstring). */
export class HubToolError extends Error {
  readonly code: string;
  readonly body: ToolErrorBody;

  constructor(toolName: string, body: ToolErrorBody) {
    super(`${toolName}: ${body.error}${body.detail ? ` -- ${body.detail}` : ""}`);
    this.name = "HubToolError";
    this.code = body.error;
    this.body = body;
  }
}

/** The request never got a tool-level answer at all: the transport
 * failed, the server is unreachable, or the connection could not be
 * established. Always safe to retry (with backoff) if you have not
 * already exhausted an attempt budget. */
export class HubConnectionError extends Error {
  readonly cause?: unknown;

  constructor(message: string, cause?: unknown) {
    super(message);
    this.name = "HubConnectionError";
    this.cause = cause;
  }
}
