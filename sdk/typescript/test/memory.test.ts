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

test("one-call turns enforce a caller deadline without sending it to the gateway", async () => {
  let payload: Record<string, unknown> = {};
  let signal: AbortSignal | undefined;
  const memory = new MemoryClient({url: "http://127.0.0.1:8765", fetch: async (_url, init) => {
    payload = JSON.parse(String(init?.body)) as Record<string, unknown>;
    signal = init?.signal as AbortSignal;
    return new Promise<Response>((_resolve, reject) => {
      const keepAlive = setTimeout(() => {}, 500);
      signal?.addEventListener("abort", () => {
        clearTimeout(keepAlive);
        reject(signal?.reason ?? new Error("aborted"));
      }, {once: true});
    });
  }});

  await assert.rejects(() => memory.turn("thread", "question", {text: "question", timeoutMs: 20}));
  assert.equal(signal?.aborted, true);
  assert.equal("timeoutMs" in payload, false);
});

test("record expiration reaping uses the bounded tenant-scoped operation", async () => {
  let payload: Record<string, unknown> = {};
  let url = "";
  const memory = new MemoryClient({url: "http://127.0.0.1:8765", agentId: "worker", fetch: async (input, init) => {
    url = String(input);
    payload = JSON.parse(String(init?.body)) as Record<string, unknown>;
    return new Response(JSON.stringify({results: [{deleted: ["expired"], reason: "retention", at: "2026-10-12T00:00:00Z"}]}));
  }});

  const result = await memory.reapDueRecords(4);
  assert.equal(url, "http://127.0.0.1:8765/v1/memory/record-reap-due");
  assert.deepEqual(payload, {limit: 4, context: ["agent:worker"]});
  assert.deepEqual(result.results[0].deleted, ["expired"]);
  assert.throws(() => memory.reapDueRecords(101), RangeError);
});

test('native middleware uses one scoped turn and replaces its trailing update', async () => {
  const {vercelMemoryMiddleware, mastraMemoryProcessor} = await import('../src/memory.js');
  const calls: Record<string, unknown>[] = [];
  const memory = new MemoryClient({url: 'http://localhost:8765', agentId: 'coding', fetch: async (_url, init) => {
    calls.push(JSON.parse(String(init?.body)));
    return new Response(JSON.stringify({context: '<memory_update>fresh</memory_update>', injected: ['tip'],
      profile: {static: [], dynamic: [], custom: []}, buffer: [], scope: {}, session_id: 's', results: []}));
  }});
  const messages = [{role: 'system', content: 'stable prefix'}, {role: 'user', content: 'earlier question'},
    {role: 'user', content: '<memory_update>old</memory_update>'}, {role: 'assistant', content: 'earlier answer'},
    {role: 'user', content: 'run pytest'}];
  const middleware = vercelMemoryMiddleware(memory, 's');
  const result = await middleware.transformParams({type: 'generate', params: {prompt: messages}});
  assert.equal(result.prompt.length, 5);
  assert.equal(result.prompt[0].content, 'stable prefix');
  assert.deepEqual(result.prompt[4].content, [{type: 'text', text: '<memory_update>fresh</memory_update>'}]);
  assert.equal(JSON.stringify(result.prompt).includes('<memory_update>old</memory_update>'), false);
  assert.equal(calls[0].query, 'run pytest');
  assert.deepEqual(calls[0].context, ['agent:coding']);
  const processor = mastraMemoryProcessor(memory, 's');
  const messageList = {clearSystemMessages: () => {}, addSystem: () => {}};
  assert.equal(await processor.processInput({messages, messageList}), messageList);
  assert.equal(calls.length, 2);
});
