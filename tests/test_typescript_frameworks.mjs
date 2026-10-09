// Native SDK factories and callbacks; no model or vendor requests are made.
import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {pathToFileURL} from "node:url";
import {MemoryClient, vercelMemoryTools, mastraMemoryTools} from "../sdk/typescript/dist/src/index.js";

const requireSDK = createRequire(`${process.env.COMMONTRACE_TS_FRAMEWORKS}/package.json`);
const ai = await import(pathToFileURL(requireSDK.resolve("ai")));
const mastra = await import(pathToFileURL(requireSDK.resolve("@mastra/core/tools")));
const {z} = await import(pathToFileURL(requireSDK.resolve("zod")));
const memory = new MemoryClient({url: "http://127.0.0.1:9000", agentId: "alice", fetch: async (url, options) => {
  const body = JSON.parse(options.body);
  assert.deepEqual(body.context, ["agent:alice"]);
  return new Response(JSON.stringify(String(url).endsWith("/reflect") ?
    {context: "Tokyo office", evidence: [], occasion_id: "alice:1"} : {facts: []}));
}});
const vercel = vercelMemoryTools(memory, ai.tool, ai.jsonSchema);
assert.equal((await vercel.recall.execute({query: "office"})).context, "Tokyo office");
assert.deepEqual((await vercel.remember.execute({text: "office fact"})).facts, []);
const tools = mastraMemoryTools(memory, mastra.createTool, {
  recall: z.object({query: z.string()}), remember: z.object({text: z.string()}),
});
assert.equal((await tools.recall.execute({query: "office"})).context, "Tokyo office");
assert.deepEqual((await tools.remember.execute({text: "office fact"})).facts, []);
console.log("Vercel and Mastra native tool factories and callbacks passed");
