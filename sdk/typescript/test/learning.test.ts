import test from "node:test";
import assert from "node:assert/strict";
import { MemoryClient } from "../src/memory.js";
import { HubClient } from "../src/client.js";
import { LEARNING_SCHEMA, learningOperations } from "../src/learning.js";

test("HTTP learning uses GET negotiation, canonical fields and pinned authority", async () => {
  const calls: {url: URL; init: RequestInit}[] = [];
  const fetcher: typeof fetch = async (input, init = {}) => {
    calls.push({url: new URL(String(input)), init});
    return new Response(JSON.stringify({schema: LEARNING_SCHEMA, operations: learningOperations}), {status: 200});
  };
  const memory = new MemoryClient({url: "http://localhost:9999", agentId: "worker", context: ["user:owner"], fetch: fetcher});
  await memory.learning.configure("adaptive", {min_arm: 10});
  await memory.learning.assign("o", ["m"], {experiment_id: "e", task_context: "pick"});
  await memory.learning.delivery("o", [], "e");
  await memory.learning.outcome("o", {reward: 0.8}, "e");
  await memory.learning.run({limit: 2});
  await memory.learning.status("e");
  await memory.learning.rollback("regression");
  for (const {url, init} of calls) {
    assert.match(url.pathname, /^\/v1\/learning\//);
    assert.equal(init.redirect, "manual");
    if (init.method === "GET") {
      assert.deepEqual(url.searchParams.getAll("context"), ["user:owner", "agent:worker"]);
      assert.equal(init.body, undefined);
    } else {
      assert.deepEqual(JSON.parse(String(init.body)).context, ["user:owner", "agent:worker"]);
    }
  }
  assert.equal(calls.find(call => call.url.pathname.endsWith("/status"))!.url.searchParams.get("experiment_id"), "e");
  const delivery = calls.find(call => call.url.pathname.endsWith("/delivery"))!;
  assert.deepEqual(JSON.parse(String(delivery.init.body)).memory_ids, []);
  const outcome = calls.find(call => call.url.pathname.endsWith("/outcome"))!;
  assert.deepEqual(JSON.parse(String(outcome.init.body)), {reward: 0.8, occasion_id: "o", experiment_id: "e", context: ["user:owner", "agent:worker"]});
});

test("MCP learning negotiates and never silently downgrades", async () => {
  const calls: string[] = [];
  let schema: string = LEARNING_SCHEMA;
  const hub = HubClient.withCaller({async callTool(name, args) {
    calls.push(name);
    return {schema, operations: learningOperations, args};
  }});
  await hub.learning.outcome("o", {metrics: {progress: 0.7}}, "e");
  assert.deepEqual(calls, ["learning_capabilities", "learning_outcome"]);
  schema = "unimem.learning.v0";
  await assert.rejects(hub.learning.run(), /support/);
  assert.deepEqual(calls, ["learning_capabilities", "learning_outcome", "learning_capabilities"]);
  await assert.rejects(async () => hub.learning.outcome("o", {reward: NaN}), /reward/);
  await assert.rejects(async () => hub.learning.outcome("o", {metrics: {bad: Infinity}}), /metrics/);
});

test("learning snapshots nested input before asynchronous negotiation", async () => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let received: Record<string, unknown> | undefined;
  const {LearningClient} = await import("../src/learning.js");
  const client = new LearningClient(async (operation, body) => {
    if (operation === "capabilities") {
      await gate;
      return {schema: LEARNING_SCHEMA, operations: learningOperations};
    }
    received = body;
    return {schema: LEARNING_SCHEMA};
  });
  const body = {occasion_id: "task", metrics: {completion: 0.8}};
  const pending = client.call("outcome", body);
  body.metrics.completion = 0.1;
  release();
  await pending;
  assert.deepEqual(received, {occasion_id: "task", metrics: {completion: 0.8}});
  assert.equal(body.metrics.completion, 0.1);
});

test("MCP learning preserves server refusal details", async () => {
  const client = HubClient.withCaller({async callTool(name) {
    return name === "learning_capabilities"
      ? {schema: LEARNING_SCHEMA, operations: learningOperations}
      : {error: "forbidden", detail: "owner required"};
  }});
  await assert.rejects(client.learning.configure("observe"), /forbidden.*owner required/);
});
