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

test('native middleware uses one scoped turn and replaces its trailing update', async () => {
  const {vercelMemoryMiddleware, mastraMemoryProcessor} = await import('../src/memory.js');
  const calls: Record<string, unknown>[] = [];
  const memory = new MemoryClient({url: 'http://localhost:8765', agentId: 'coding', fetch: async (_url, init) => {
    calls.push(JSON.parse(String(init?.body)));
    return new Response(JSON.stringify({context: '<memory_update>fresh</memory_update>', injected: ['tip'],
      profile: {static: [], dynamic: [], custom: []}, buffer: [], scope: {}, session_id: 's', results: []}));
  }});
  const messages = [{role: 'system', content: 'stable prefix'}, {role: 'user', content: 'run pytest'},
    {role: 'user', content: '<memory_update>old</memory_update>'}];
  const middleware = vercelMemoryMiddleware(memory, 's');
  const result = await middleware.transformParams({type: 'generate', params: {prompt: messages}});
  assert.equal(result.prompt.length, 3);
  assert.equal(result.prompt[0].content, 'stable prefix');
  assert.deepEqual(result.prompt[2].content, [{type: 'text', text: '<memory_update>fresh</memory_update>'}]);
  assert.equal(calls[0].query, 'run pytest');
  assert.deepEqual(calls[0].context, ['agent:coding']);
  const processor = mastraMemoryProcessor(memory, 's');
  assert.equal((await processor.processInput({messages})).length, 3);
  assert.equal(calls.length, 2);
});
