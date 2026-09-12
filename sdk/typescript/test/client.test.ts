import assert from "node:assert/strict";
import { test } from "node:test";

import { HubClient, HubToolError, type ToolCaller } from "../src/index.js";

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
