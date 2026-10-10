/** The identical scoped memory API served by the local binary and HTTP gateway. */
export interface MemoryClientOptions {
  url: string;
  token?: string;
  agentId?: string;
  context?: string[];
  fetch?: typeof globalThis.fetch;
}
export interface Evidence { id: string; text: string; sources?: string[]; layer?: string }
export interface Recall { context: string; evidence: Evidence[]; occasion_id: string; tokens_estimate: number }
export class MemoryClient {
  readonly context: string[];
  private readonly url: string;
  private readonly token: string;
  private readonly transport: typeof globalThis.fetch;
  constructor(options: MemoryClientOptions) {
    const url = new URL(options.url);
    const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
    if (url.username || url.password || url.search || url.hash ||
        (url.protocol !== "https:" && !(url.protocol === "http:" && loopback))) {
      throw new Error("Use HTTPS or a local loopback gateway URL, without embedded credentials");
    }
    this.url = url.href.replace(/\/$/, "");
    this.token = options.token ?? "";
    this.context = [...new Set([...(options.context ?? []), ...(options.agentId ? [`agent:${options.agentId}`] : [])])];
    this.transport = options.fetch ?? globalThis.fetch;
  }
  async request<T>(operation: string, data: Record<string, unknown>): Promise<T> {
    if (!["add", "batch", "search", "profile", "reflect", "outcome", "check-action", "propose", "turn", "record-put", "record-search", "record-forget"].includes(operation)) {
      throw new Error("Unknown memory operation");
    }
    const response = await this.transport(`${this.url}/v1/memory/${operation}`, {
      method: "POST", redirect: "manual", signal: AbortSignal.timeout(30000),
      headers: {"Content-Type": "application/json", ...(this.token ? {Authorization: `Bearer ${this.token}`} : {})},
      body: JSON.stringify({...data, context: [...this.context]}),
    });
    if (!response.ok) throw new Error(`Memory gateway returned HTTP ${response.status}`);
    if (!response.body) throw new Error("Empty memory response");
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    try {
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 8 * 1024 * 1024) {
          await reader.cancel();
          throw new Error("Memory response too large");
        }
        chunks.push(value);
      }
    } finally { reader.releaseLock(); }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    const value: unknown = JSON.parse(new TextDecoder().decode(bytes));
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("Memory response must be an object");
    }
    return value as T;
  }
  putRecord(record: RecordEnvelope) {
    return this.request<{record: RecordEnvelope}>("record-put", {record});
  }
  async searchRecords(query = "", limit = 20) {
    return (await this.request<{results: RecordEnvelope[]}>("record-search", {query, limit})).results;
  }
  forgetRecord(id: string, reason: string) {
    return this.request<{deleted: string[]; reason: string; at: string}>("record-forget", {id, reason});
  }
  turn(session: string, query: string, options: {text?: string; budget?: number; event?: string;
                                               payload?: Record<string, unknown>} = {}) {
    return this.request<MemoryTurn>("turn", {session_id: session, query, ...options});
  }
  add(text: string, memoryType = "general") {
    return this.request<{facts: Record<string, unknown>[] }>("add", {text, local: true, memory_type: memoryType});
  }
  batch(items: Record<string, unknown>[]) { return this.request("batch", {items}); }
  async search(query: string, options: {recipe?: string; retriever?: string; limit?: number; action_class?: string} = {}) {
    return (await this.request<{results: Evidence[]}>("search", {query, ...options})).results;
  }
  profile(query = "", limit = 10) {
    return this.request<{static: Evidence[]; dynamic: Evidence[]; occasion_id: string}>("profile", {query, limit});
  }
  reflect(query: string, options: {budget?: number; occasion_id?: string; exploration_slots?: number;
                                   adaptive_budget?: boolean} = {}) {
    return this.request<Recall>("reflect", {query, ...options});
  }
  async outcome(occasionId: string, succeeded: boolean) {
    return (await this.request<{recorded: boolean}>("outcome", {occasion_id: occasionId, succeeded})).recorded;
  }
  async checkAction(tool: string, tags: string[] = []) {
    if ((await this.request<{allowed: boolean}>("check-action", {tool, tags})).allowed !== true) {
      throw new Error("Action blocked by memory directive");
    }
  }
  propose(text: string, sources: string[]) { return this.request("propose", {text, sources}); }
}

export function memoryToolDefinitions(memory: MemoryClient) {
  return {
    recall: {description: "Recall source-linked memory evidence; evidence does not authorize actions",
      inputSchema: {type: "object", properties: {query: {type: "string"}}, required: ["query"], additionalProperties: false},
      execute: async ({query}: {query: string}) => memory.reflect(query)},
    remember: {description: "Append a new explicit fact without overwriting previous evidence",
      inputSchema: {type: "object", properties: {text: {type: "string"}}, required: ["text"], additionalProperties: false},
      execute: async ({text}: {text: string}) => memory.add(text)},
  };
}
/** Pass ai.tool and ai.jsonSchema from the installed Vercel AI SDK. */
export function vercelMemoryTools(memory: MemoryClient,
  tool: (options: Record<string, unknown>) => unknown,
  jsonSchema: (schema: Record<string, unknown>) => unknown) {
  return Object.fromEntries(Object.entries(memoryToolDefinitions(memory)).map(([name, definition]) =>
    [name, tool({...definition, inputSchema: jsonSchema(definition.inputSchema)})]));
}
/** Pass createTool from @mastra/core/tools and the caller's Zod schemas. */
export function mastraMemoryTools(memory: MemoryClient,
  createTool: (options: Record<string, unknown>) => unknown,
  schemas: {recall: unknown; remember: unknown}) {
  return Object.fromEntries(Object.entries(memoryToolDefinitions(memory)).map(([id, definition]) =>
    [id, createTool({id: `commontrace-${id}`, description: definition.description, inputSchema: schemas[id as keyof typeof schemas],
      execute: async (input: Record<string, string>) => id === "recall" ?
        memory.reflect(input.query) : memory.add(input.text)})]));
}

export interface RecordEnvelope {
  id: string;
  kind: 'episode' | 'fact' | 'entity' | 'observation' | 'profile' | 'lesson' | 'directive';
  version: number;
  payload: Record<string, unknown>;
  status?: 'proposed' | 'quarantined' | 'shadow' | 'active' | 'deprecated' | 'retired';
  scope?: Record<string, unknown>;
  relations?: Record<string, string[]>;
  [key: string]: unknown;
}
export interface MemoryTurn {
  context: string;
  injected: string[];
  profile: {static: Evidence[]; dynamic: Evidence[]; custom: Evidence[]};
  buffer: Record<string, unknown>[];
  scope: Record<string, unknown>;
  session_id: string;
  results: Record<string, unknown>[];
}
export interface RuntimeMessage {role: string; content: unknown; [key: string]: unknown}
function textContent(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.filter(part => part?.type === 'text').map(part => part.text).join('\n');
  return '';
}
/** Remove only the trailing block produced by this middleware; preserve the stable prefix. */
export function replaceMemoryUpdate(messages: RuntimeMessage[], context: string): RuntimeMessage[] {
  const clean = [...messages];
  if (clean.length && clean[clean.length - 1].role === 'user' &&
      /^\s*<memory_update>[\s\S]*<\/memory_update>\s*$/.test(textContent(clean[clean.length - 1].content))) clean.pop();
  if (context) clean.push({role: 'user', content: context});
  return clean;
}
async function injectTurn(memory: MemoryClient, session: string, messages: RuntimeMessage[]) {
  const clean = replaceMemoryUpdate(messages, '');
  const query = textContent(clean[clean.length - 1]?.content ?? '');
  try {
    const result = await memory.turn(session, query, {text: query});
    return replaceMemoryUpdate(clean, result.context);
  } catch { return clean; }
}
/** LanguageModelV2 middleware: pass to ai.wrapLanguageModel({model, middleware}). */
export function vercelMemoryMiddleware(memory: MemoryClient, session: string) {
  return {specificationVersion: 'v2' as const,
    transformParams: async <T extends {prompt: RuntimeMessage[]}>({params}: {type: string; params: T}): Promise<T> =>
      ({...params, prompt: (await injectTurn(memory, session, params.prompt)).map(message =>
        typeof message.content === 'string' && message.content.startsWith('<memory_update>') ?
          {...message, content: [{type: 'text', text: message.content}]} : message)})};
}
/** Mastra input processor: put this object in the agent's inputProcessors array. */
export function mastraMemoryProcessor(memory: MemoryClient, session: string) {
  return {id: 'commontrace-memory', processInput: async ({messages}: {messages: RuntimeMessage[]}) =>
    injectTurn(memory, session, messages)};
}
