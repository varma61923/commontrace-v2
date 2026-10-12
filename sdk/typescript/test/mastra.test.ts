import {test} from "node:test";
import assert from "node:assert/strict";
import {MemoryClient, mastraMemoryOutputProcessor, mastraMemoryProcessor} from "../src/memory.js";

function client(response: (payload: Record<string, unknown>) => Response) {
  const payloads: Record<string, unknown>[] = [];
  const memory = new MemoryClient({url: "http://127.0.0.1:8765", agentId: "support", fetch: async (_url, init) => {
    const payload = JSON.parse(String(init?.body)) as Record<string, unknown>;
    payloads.push(payload);
    return response(payload);
  }});
  return {memory, payloads};
}

function turnResponse(context = "<memory_update>read-only evidence</memory_update>") {
  return new Response(JSON.stringify({context, injected: [], profile: {static: [], dynamic: [], custom: []},
    buffer: [], scope: {}, session_id: "thread-1", results: []}));
}

test("Mastra input processor uses MessageList tags and request-scoped thread identity", async () => {
  const {memory, payloads} = client(() => turnResponse());
  const cleared: string[] = [];
  const added: Array<{text: string; tag?: string}> = [];
  const messageList = {
    clearSystemMessages: (tag: string) => cleared.push(tag),
    addSystem: (text: string, tag?: string) => added.push({text, tag}),
  };
  const requestContext = {get: (key: string) => key === "mastra__threadId" ? "thread-9" :
    key === "userId" ? "alice" : undefined};
  const processor = mastraMemoryProcessor(memory, "fallback-thread", {
    getSessionId: context => (context as typeof requestContext | undefined)?.get("mastra__threadId"),
    getContext: context => {
      const userId = (context as typeof requestContext | undefined)?.get("userId");
      return typeof userId === "string" ? [`user:${userId}`] : undefined;
    },
  });
  const messages = [{role: "user", content: {parts: [{type: "text", text: "Where is the runbook?"}]}}];

  const result = await processor.processInput({messages, messageList, requestContext});

  assert.equal(result, messageList);
  assert.deepEqual(cleared, ["commontrace-memory"]);
  assert.deepEqual(added, [{text: "<memory_update>read-only evidence</memory_update>", tag: "commontrace-memory"}]);
  assert.equal(payloads.length, 1);
  assert.equal(payloads[0].session_id, "thread-9");
  assert.equal(payloads[0].query, "Where is the runbook?");
  assert.equal(payloads[0].text, "Where is the runbook?");
  assert.deepEqual(payloads[0].context, ["agent:support", "user:alice"]);
});

test("Mastra skips shared-client retrieval when request principal scope is missing", async () => {
  const {memory, payloads} = client(() => turnResponse());
  const messageList = {clearSystemMessages: () => {}, addSystem: () => {}};
  const processor = mastraMemoryProcessor(memory, "fallback-thread", {
    getSessionId: () => "thread-1",
    getContext: () => undefined,
  });
  const messages = [{role: "user", content: "question"}];

  assert.equal(await processor.processInput({messages, messageList}), messageList);
  assert.equal(payloads.length, 0);
});

test("Mastra input processor clears stale memory and fails open on retrieval errors", async () => {
  const {memory} = client(() => new Response("unavailable", {status: 503}));
  const cleared: string[] = [];
  const added: string[] = [];
  const messageList = {
    clearSystemMessages: (tag: string) => cleared.push(tag),
    addSystem: (text: string) => added.push(text),
  };
  const processor = mastraMemoryProcessor(memory, "thread-1");
  const messages = [{role: "user", content: {parts: [{type: "text", text: "new question"}]}}];

  const result = await processor.processInput({messages, messageList});

  assert.equal(result, messageList);
  assert.deepEqual(cleared, ["commontrace-memory"]);
  assert.deepEqual(added, []);
});

test("Mastra output processor captures the final assistant text and preserves messages", async () => {
  const {memory, payloads} = client(() => turnResponse(""));
  const processor = mastraMemoryOutputProcessor(memory, "thread-1");
  const messages = [
    {role: "user", content: {parts: [{type: "text", text: "question"}]}},
    {role: "assistant", content: {parts: [{type: "text", text: "answer"}]}, id: "answer-1"},
  ];

  const result = await processor.processOutputResult({messages});

  assert.equal(result, messages);
  assert.equal(payloads.length, 1);
  assert.equal(payloads[0].event, "SessionEnd");
  assert.deepEqual(payloads[0].payload, {response: "answer", succeeded: true});
  assert.equal(payloads[0].budget, 0);
});

test("Mastra output processor fails open when completion capture is unavailable", async () => {
  const {memory} = client(() => new Response("unavailable", {status: 503}));
  const processor = mastraMemoryOutputProcessor(memory, "thread-1");
  const messages = [{role: "assistant", content: {parts: [{type: "text", text: "answer"}]}}];

  assert.equal(await processor.processOutputResult({messages}), messages);
});

test("Mastra output capture is detached from result delivery", async () => {
  let requested = false;
  const memory = new MemoryClient({url: "http://127.0.0.1:8765", fetch: async () => {
    requested = true;
    return new Promise<Response>(() => {});
  }});
  const processor = mastraMemoryOutputProcessor(memory, "thread-1");
  const messages = [{role: "assistant", content: {parts: [{type: "text", text: "answer"}]}}];

  const result = await processor.processOutputResult({messages});

  assert.equal(result, messages);
  assert.equal(requested, true);
});
