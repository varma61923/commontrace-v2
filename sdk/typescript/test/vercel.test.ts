import {test} from "node:test";
import assert from "node:assert/strict";
import {MemoryClient, vercelMemoryMiddleware} from "../src/memory.js";

function memoryClient(replies: string[]) {
  const payloads: Record<string, unknown>[] = [];
  const memory = new MemoryClient({
    url: "http://127.0.0.1:8765",
    agentId: "coding",
    fetch: async (_url, init) => {
      payloads.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      const context = replies.shift();
      if (context === undefined) throw new Error("unexpected memory request");
      return new Response(JSON.stringify({context, injected: [],
        profile: {static: [], dynamic: [], custom: []}, buffer: [], scope: {}, session_id: "s", results: []}));
    },
  });
  return {memory, payloads};
}

test("Vercel middleware uses the current V4 contract and injects once per user turn", async () => {
  const {memory, payloads} = memoryClient([
    "<memory_update>first evidence</memory_update>",
    "<memory_update>second evidence</memory_update>",
  ]);
  const middleware = vercelMemoryMiddleware(memory, "thread-1");
  assert.equal(middleware.specificationVersion, "v4");

  const first = await middleware.transformParams({type: "generate", params: {prompt: [
    {role: "system", content: "stable instructions"},
    {role: "user", content: "older question"},
    {role: "user", content: "<memory_update>stale evidence</memory_update>"},
    {role: "assistant", content: [{type: "text", text: "older answer"}]},
    {role: "user", content: [{type: "text", text: "current question"}]},
  ], temperature: 0.2}});

  assert.equal(payloads.length, 1);
  assert.equal(payloads[0].query, "current question");
  assert.equal(payloads[0].text, "current question");
  assert.deepEqual(payloads[0].context, ["agent:coding"]);
  assert.equal(first.temperature, 0.2);
  assert.equal(first.prompt.some(message => JSON.stringify(message).includes("stale evidence")), false);
  assert.deepEqual(first.prompt.at(-1)?.content,
    [{type: "text", text: "<memory_update>first evidence</memory_update>"}]);

  const toolContinuation = await middleware.transformParams({type: "stream", params: {prompt: [
    ...first.prompt,
    {role: "assistant", content: [{type: "tool-call", toolCallId: "c1", toolName: "read", input: {}}]},
    {role: "tool", content: [{type: "tool-result", toolCallId: "c1", output: "done"}]},
  ]}});
  assert.equal(payloads.length, 1);
  assert.equal(JSON.stringify(toolContinuation.prompt).includes("first evidence"), true);

  const nextTurn = await middleware.transformParams({type: "generate", params: {prompt: [
    ...toolContinuation.prompt,
    {role: "assistant", content: [{type: "text", text: "finished"}]},
    {role: "user", content: "next question"},
  ]}});
  assert.equal(payloads.length, 2);
  assert.equal(payloads[1].query, "next question");
  assert.equal(JSON.stringify(nextTurn.prompt).includes("first evidence"), false);
  assert.equal(JSON.stringify(nextTurn.prompt).includes("second evidence"), true);
});

test("Vercel retrieval failures fail open after removing stale turn context", async () => {
  const memory = new MemoryClient({
    url: "http://127.0.0.1:8765",
    fetch: async () => new Response("unavailable", {status: 503}),
  });
  const middleware = vercelMemoryMiddleware(memory, "thread-1");
  const params = {prompt: [
    {role: "system", content: "instructions"},
    {role: "user", content: "<memory_update>stale evidence</memory_update>"},
    {role: "assistant", content: "previous answer"},
    {role: "user", content: "new question"},
  ]};

  const result = await middleware.transformParams({type: "generate", params});
  assert.deepEqual(result.prompt, [
    {role: "system", content: "instructions"},
    {role: "assistant", content: "previous answer"},
    {role: "user", content: "new question"},
  ]);
});

test("Vercel middleware removes stale context when the current turn has no session ID", async () => {
  const {memory, payloads} = memoryClient([]);
  const middleware = vercelMemoryMiddleware(memory, " ");
  const result = await middleware.transformParams({type: "generate", params: {prompt: [
    {role: "user", content: "earlier question"},
    {role: "user", content: "<memory_update>stale evidence</memory_update>"},
    {role: "assistant", content: "earlier answer"},
    {role: "user", content: "current question"},
  ]}});

  assert.equal(payloads.length, 0);
  assert.equal(result.prompt.some(message => JSON.stringify(message).includes("stale evidence")), false);
  assert.equal(result.prompt.at(-1)?.content, "current question");
});

test("Vercel middleware captures only the final generated answer after tool calls", async () => {
  const {memory, payloads} = memoryClient([
    "<memory_update>evidence</memory_update>",
    "",
  ]);
  const middleware = vercelMemoryMiddleware(memory, "thread-1");
  const transformed = await middleware.transformParams({type: "generate", params: {prompt: [
    {role: "user", content: "question"},
  ]}});
  const toolCall = {content: [{type: "tool-call", toolCallId: "c1", toolName: "lookup", input: "{}"}],
    finishReason: {unified: "tool-calls", raw: "tool_calls"}};
  await middleware.wrapGenerate?.({doGenerate: async () => toolCall,
    doStream: async () => ({stream: new ReadableStream()}), params: transformed, model: {}});
  assert.equal(payloads.filter(payload => payload.event === "SessionEnd").length, 0);

  const answer = {content: [{type: "text", text: "the answer"}],
    finishReason: {unified: "stop", raw: "stop"}};
  const result = await middleware.wrapGenerate?.({doGenerate: async () => answer,
    doStream: async () => ({stream: new ReadableStream()}), params: transformed, model: {}});
  assert.equal(result, answer);
  assert.equal(payloads.filter(payload => payload.event === "SessionEnd").length, 1);
  assert.deepEqual(payloads.at(-1)?.payload, {response: "the answer", succeeded: true});
});

test("Vercel stream capture taps text without changing emitted parts and fails open", async () => {
  const {memory, payloads} = memoryClient([
    "<memory_update>evidence</memory_update>",
    "",
  ]);
  const middleware = vercelMemoryMiddleware(memory, "thread-1");
  const transformed = await middleware.transformParams({type: "stream", params: {prompt: [
    {role: "user", content: [{type: "text", text: "question"}]},
  ]}});
  const parts = [
    {type: "text-delta", id: "text-1", delta: "the "},
    {type: "text-delta", id: "text-1", delta: "answer"},
    {type: "finish", finishReason: {unified: "stop", raw: "stop"},
      usage: {inputTokens: {}, outputTokens: {}}},
  ];
  const source = new ReadableStream({start(controller) {
    for (const part of parts) controller.enqueue(part);
    controller.close();
  }});
  const wrapped = await middleware.wrapStream?.({doStream: async () => ({stream: source, marker: "preserved"}),
    doGenerate: async () => ({}), params: transformed, model: {}});
  assert.equal(wrapped?.marker, "preserved");
  const output = [];
  for await (const part of wrapped!.stream) output.push(part);
  assert.deepEqual(output, parts);
  assert.deepEqual(payloads.at(-1)?.payload, {response: "the answer", succeeded: true});
});

test("Vercel completion capture outage does not replace a successful model result", async () => {
  const memory = new MemoryClient({url: "http://127.0.0.1:8765", fetch: async () =>
    new Response("unavailable", {status: 503})});
  const middleware = vercelMemoryMiddleware(memory, "thread-1");
  const answer = {content: [{type: "text", text: "still delivered"}],
    finishReason: {unified: "stop", raw: "stop"}};
  const result = await middleware.wrapGenerate?.({doGenerate: async () => answer,
    doStream: async () => ({stream: new ReadableStream()}),
    params: {prompt: [{role: "user", content: "question"}]}, model: {}});
  assert.equal(result, answer);
});

test("Vercel marks truncated final output unsuccessful", async () => {
  const {memory, payloads} = memoryClient([""]);
  const middleware = vercelMemoryMiddleware(memory, "thread-1");
  const result = await middleware.wrapGenerate?.({doGenerate: async () => ({
    content: [{type: "text", text: "truncated answer"}],
    finishReason: {unified: "length", raw: "length"},
  }), doStream: async () => ({stream: new ReadableStream()}),
  params: {prompt: [{role: "user", content: "question"}]}, model: {}});

  assert.equal(result?.finishReason.unified, "length");
  assert.deepEqual(payloads.at(-1)?.payload, {response: "truncated answer", succeeded: false});
});
