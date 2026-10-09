import {test} from "node:test";
import assert from "node:assert/strict";
import {MemoryClient} from "../src/memory.js";

test("memory API pins principal scope and refuses redirects", async () => {
  let payload: Record<string, unknown> = {};
  const transport: typeof fetch = async (url, options) => {
    assert.equal(String(url), "http://127.0.0.1:9000/v1/memory/reflect");
    assert.equal(options?.redirect, "manual");
    payload = JSON.parse(String(options?.body));
    return new Response(JSON.stringify({context: "evidence", evidence: [], occasion_id: "a:1"}));
  };
  const memory = new MemoryClient({url: "http://127.0.0.1:9000", agentId: "a", fetch: transport});
  assert.equal((await memory.reflect("question")).context, "evidence");
  assert.deepEqual(payload.context, ["agent:a"]);
  assert.throws(() => new MemoryClient({url: "http://remote.example"}));
  const redirect = new MemoryClient({url: "https://memory.example", fetch: async () => new Response(null, {status: 302})});
  await assert.rejects(() => redirect.add("fact"), /302/);
});

test("malformed action authorization and nonobject responses fail closed", async () => {
  const memory = new MemoryClient({url: "http://127.0.0.1:9000", fetch: async () =>
    new Response('{"allowed":"false"}')});
  await assert.rejects(() => memory.checkAction("refund"), /blocked/);
  for (const body of ["null", "[]", "false"]) {
    const invalid = new MemoryClient({url: "http://127.0.0.1:9000", fetch: async () => new Response(body)});
    await assert.rejects(() => invalid.add("fact"), /must be an object/);
  }
});
