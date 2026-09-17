import assert from "node:assert/strict";
import { mock, test } from "node:test";

import { clampRetryAfter, parseToolResult } from "../src/client.js";
import { HubClient, HubConnectionError, HubToolError, type ToolCaller } from "../src/index.js";

/** A fake caller: no network, no MCP server, no real Hub -- exactly the
 * seam `HubClient.withCaller` exists for. Records every call it saw so
 * a test can assert what the client actually sent. */
class FakeCaller implements ToolCaller {
  calls: { name: string; args: Record<string, unknown> }[] = [];
  private responses: Record<string, unknown>[];

  constructor(...responses: Record<string, unknown>[]) {
    this.responses = responses;
  }

  async callTool(name: string, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    this.calls.push({ name, args });
    if (this.responses.length === 0) {
      throw new Error("FakeCaller ran out of scripted responses");
    }
    return this.responses.shift() as Record<string, unknown>;
  }
}

test("call() returns the body when the tool succeeds", async () => {
  const caller = new FakeCaller({ id: "t1", title: "x" });
  const client = HubClient.withCaller(caller);
  const result = await client.call("get_trace", { id: "t1" });
  assert.deepEqual(result, { id: "t1", title: "x" });
  assert.equal(caller.calls.length, 1);
  assert.equal(caller.calls[0].name, "get_trace");
});

test("a non-rate-limited tool error is raised as HubToolError immediately, not retried", async () => {
  const caller = new FakeCaller({ error: "not_found", detail: "no trace with id t1" });
  const client = HubClient.withCaller(caller);
  await assert.rejects(
    () => client.call("get_trace", { id: "t1" }),
    (err: unknown) => {
      assert.ok(err instanceof HubToolError);
      assert.equal(err.code, "not_found");
      assert.match(err.message, /no trace with id t1/);
      return true;
    },
  );
  assert.equal(caller.calls.length, 1, "a permission/lookup failure must not be retried");
});

test("a rate_limited error is retried and eventually succeeds", async () => {
  const caller = new FakeCaller(
    { error: "rate_limited", retry_after: 0 },
    { error: "rate_limited", retry_after: 0 },
    { id: "t1", title: "eventually" },
  );
  const client = HubClient.withCaller(caller, { maxAttempts: 5 });
  const result = await client.call("contribute_trace", {});
  assert.deepEqual(result, { id: "t1", title: "eventually" });
  assert.equal(caller.calls.length, 3);
});

test("a rate_limited error gives up as HubToolError once maxAttempts is spent", async () => {
  const caller = new FakeCaller(
    { error: "rate_limited", retry_after: 0 },
    { error: "rate_limited", retry_after: 0 },
  );
  const client = HubClient.withCaller(caller, { maxAttempts: 2 });
  await assert.rejects(
    () => client.call("contribute_trace", {}),
    (err: unknown) => {
      assert.ok(err instanceof HubToolError);
      assert.equal(err.code, "rate_limited");
      return true;
    },
  );
  assert.equal(caller.calls.length, 2, "must not exceed maxAttempts");
});

test("searchTraces forwards args and returns the typed result shape", async () => {
  const caller = new FakeCaller({
    traces: [], limit: 20, offset: 0, has_more: false, terms: ["x"], terms_ignored: [],
  });
  const client = HubClient.withCaller(caller);
  const result = await client.searchTraces({ query: "how do I x" });
  assert.equal(caller.calls[0].name, "search_traces");
  assert.deepEqual(caller.calls[0].args, { query: "how do I x" });
  assert.deepEqual(result.traces, []);
  assert.equal(result.has_more, false);
});

test("voteTrace sends the full payload shape the tool expects", async () => {
  const caller = new FakeCaller({ id: "t1", trust: 1.0 });
  const client = HubClient.withCaller(caller);
  await client.voteTrace("t1", "up");
  assert.deepEqual(caller.calls[0].args, {
    id: "t1", vote: "up", feedback_tag: "", feedback_text: "",
  });
});

test("accountUsage calls the tool with no arguments", async () => {
  const caller = new FakeCaller({
    plan: "free", period: "2026-09",
    commons_queries: { used: 0, allowance: 20, remaining: 20, bonus_from_accepted_submissions: 0 },
    traces: { used: 0, limit: 1000 },
    agents: {
      active: 0, named: 0, unattributed_agent_id: "unattributed",
      unattributed_traces: 0, is_floor: false, window_days: 30, limit: 5,
    },
  });
  const client = HubClient.withCaller(caller);
  const usage = await client.accountUsage();
  assert.equal(caller.calls[0].name, "account_usage");
  assert.deepEqual(caller.calls[0].args, {});
  assert.equal(usage.plan, "free");
});

test("listTags calls the tool with no arguments and returns its tags array", async () => {
  const caller = new FakeCaller({ tags: ["billing", "auth"] });
  const client = HubClient.withCaller(caller);
  const result = await client.listTags();
  assert.deepEqual(result.tags, ["billing", "auth"]);
});

test("parseToolResult converts plain-text isError to tool_error", () => {
  const result = parseToolResult("contribute_trace", {
    isError: true,
    content: [{ type: "text", text: "Internal Server Error: backend database is offline" }],
  });
  assert.deepEqual(result, {
    error: "tool_error",
    detail: "Internal Server Error: backend database is offline",
  });
});

test("parseToolResult converts invalid JSON isError to tool_error", () => {
  const result = parseToolResult("get_trace", {
    isError: true,
    content: [{ type: "text", text: "Error: 502 Bad Gateway {broken json" }],
  });
  assert.deepEqual(result, {
    error: "tool_error",
    detail: "Error: 502 Bad Gateway {broken json",
  });
});

test("parseToolResult preserves structured JSON error when isError is true", () => {
  const result = parseToolResult("contribute_trace", {
    isError: true,
    content: [{ type: "text", text: JSON.stringify({ error: "rate_limited", retry_after: 5 }) }],
  });
  assert.deepEqual(result, { error: "rate_limited", retry_after: 5 });
});

test("parseToolResult throws HubConnectionError on invalid JSON when isError is false", () => {
  assert.throws(
    () => parseToolResult("get_trace", { isError: false, content: [{ type: "text", text: "not json" }] }),
    (err: unknown) => {
      assert.ok(err instanceof HubConnectionError);
      assert.match(err.message, /response was not valid JSON/);
      return true;
    },
  );
});

test("non-JSON tool error (tool_error) is raised as HubToolError without retry", async () => {
  const caller = new FakeCaller({
    error: "tool_error",
    detail: "502 Bad Gateway: upstream server unavailable",
  });
  const client = HubClient.withCaller(caller);
  await assert.rejects(
    () => client.call("get_trace", { id: "t1" }),
    (err: unknown) => {
      assert.ok(err instanceof HubToolError);
      assert.equal(err.code, "tool_error");
      assert.match(err.message, /upstream server unavailable/);
      return true;
    },
  );
  assert.equal(caller.calls.length, 1, "tool_error must not be retried");
});

test("clampRetryAfter clamps delay between 1 and 30 seconds", () => {
  assert.equal(clampRetryAfter(3600), 30, "values > 30s must be clamped to 30s");
  assert.equal(clampRetryAfter(60), 30, "values > 30s must be clamped to 30s");
  assert.equal(clampRetryAfter(30), 30, "exact 30s should be preserved");
  assert.equal(clampRetryAfter(15), 15, "values between 1 and 30 should be preserved");
  assert.equal(clampRetryAfter(1), 1, "exact 1s should be preserved");
  assert.equal(clampRetryAfter(0), 1, "values < 1s must be clamped to minimum 1s");
  assert.equal(clampRetryAfter(-10), 1, "negative values must be clamped to minimum 1s");
  assert.equal(clampRetryAfter(undefined), 1, "undefined must default to 1s");
  assert.equal(clampRetryAfter("invalid"), 1, "non-number must default to 1s");
  assert.equal(clampRetryAfter(NaN), 1, "NaN must default to 1s");
  assert.equal(clampRetryAfter(Infinity), 30, "Infinity must clamp to 30s");
  assert.equal(clampRetryAfter(-Infinity), 1, "-Infinity must clamp to 1s");
  assert.equal(clampRetryAfter(null), 1, "null must default to 1s");
  assert.equal(clampRetryAfter({}), 1, "object must default to 1s");
});

test("parseToolResult normalizes structuredContent with non-string error field to tool_error", () => {
  const nestedResult = parseToolResult("test_tool", {
    isError: true,
    structuredContent: {
      error: { code: "UNAUTHORIZED", message: "No access" },
      status: 403,
    },
  });
  assert.equal(nestedResult.error, "tool_error");
  assert.equal(nestedResult.status, 403);
  assert.equal(typeof nestedResult.detail, "string");

  const numericResult = parseToolResult("test_tool", {
    isError: true,
    structuredContent: {
      error: 500,
      message: "Internal Server Error",
    },
  });
  assert.equal(numericResult.error, "tool_error");
  assert.equal(numericResult.message, "Internal Server Error");
  assert.equal(typeof numericResult.detail, "string");
});

test("client.call throws HubToolError on structuredContent with nested or non-string error", async () => {
  const parsed = parseToolResult("test_tool", {
    isError: true,
    structuredContent: {
      error: { code: "UNAUTHORIZED", message: "No access" },
      status: 403,
    },
  });
  const client = HubClient.withCaller(new FakeCaller(parsed));
  await assert.rejects(
    () => client.call("test_tool"),
    (err: unknown) => {
      assert.ok(err instanceof HubToolError);
      assert.equal(err.code, "tool_error");
      return true;
    },
  );

  const numericParsed = parseToolResult("test_tool", {
    isError: true,
    structuredContent: {
      error: 500,
      message: "Internal Server Error",
    },
  });
  const clientNumeric = HubClient.withCaller(new FakeCaller(numericParsed));
  await assert.rejects(
    () => clientNumeric.call("test_tool"),
    (err: unknown) => {
      assert.ok(err instanceof HubToolError);
      assert.equal(err.code, "tool_error");
      return true;
    },
  );
});

test("client.call handles valid JSON null response without TypeError", async () => {
  const caller = new FakeCaller(null as unknown as Record<string, unknown>);
  const client = HubClient.withCaller(caller);
  const result = await client.call("test_tool");
  assert.equal(result, null);
});



