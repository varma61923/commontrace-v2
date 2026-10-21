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
  async request<T>(operation: string, data: Record<string, unknown>, options: {
    timeoutMs?: number; context?: string[];
  } = {}): Promise<T> {
    if (!["add", "batch", "search", "profile", "reflect", "outcome", "check-action", "propose", "turn", "record-put", "record-search", "record-forget", "record-reap-due"].includes(operation)) {
      throw new Error("Unknown memory operation");
    }
    const timeoutMs = options.timeoutMs ?? 30000;
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 30000) {
      throw new RangeError("Memory request timeout must be an integer from 1 to 30000 milliseconds");
    }
    const context = [...new Set([...this.context, ...(options.context ?? [])])];
    const response = await this.transport(`${this.url}/v1/memory/${operation}`, {
      method: "POST", redirect: "manual", signal: AbortSignal.timeout(timeoutMs),
      headers: {"Content-Type": "application/json", ...(this.token ? {Authorization: `Bearer ${this.token}`} : {})},
      body: JSON.stringify({...data, context}),
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
  reapDueRecords(limit = 16) {
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) {
      throw new RangeError("Expiration batch limit must be an integer from 1 to 100");
    }
    return this.request<{results: {deleted: string[]; reason: string; at: string}[]}>(
      "record-reap-due", {limit});
  }
  turn(session: string, query: string, options: {text?: string; budget?: number; event?: string;
                                               payload?: Record<string, unknown>; timeoutMs?: number;
                                               context?: string[]} = {}) {
    const {timeoutMs, context, ...data} = options;
    return this.request<MemoryTurn>("turn", {session_id: session, query, ...data}, {timeoutMs, context});
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
    [id, createTool({id: `unimem-${id}`, description: definition.description, inputSchema: schemas[id as keyof typeof schemas],
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
export interface PromptMessage {role: string; content: unknown}
export interface RuntimeMessage extends PromptMessage {[key: string]: unknown}
function textContent(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.filter(part => part?.type === 'text' && typeof part.text === 'string')
    .map(part => part.text as string).join('\n');
  if (content !== null && typeof content === 'object') {
    const value = content as {content?: unknown; parts?: unknown};
    if (typeof value.content === 'string') return value.content;
    if (Array.isArray(value.parts)) return textContent(value.parts);
  }
  return '';
}
const MEMORY_UPDATE = /^\s*<memory_update>[\s\S]*<\/memory_update>\s*$/;
function isMemoryUpdate(message: PromptMessage): boolean {
  return message.role === 'user' && MEMORY_UPDATE.test(textContent(message.content));
}
/** Remove prior middleware blocks, preserving every host-owned prompt message. */
export function replaceMemoryUpdate(messages: PromptMessage[], context: string): PromptMessage[] {
  const clean = messages.filter(message => !isMemoryUpdate(message));
  if (context) clean.push({role: 'user', content: context});
  return clean;
}
async function injectTurn(memory: MemoryClient, session: string, messages: PromptMessage[]) {
  const last = messages[messages.length - 1];
  // The AI SDK calls middleware once for each tool-loop model step. Only a new
  // user turn performs capture + retrieval; preserve the current turn's block
  // through assistant/tool continuations without querying again.
  if (!last || last.role !== 'user' || isMemoryUpdate(last)) return messages;
  const clean = replaceMemoryUpdate(messages, '');
  if (!session.trim()) return clean;
  const query = textContent(last.content);
  if (!query.trim()) return clean;
  try {
    const result = await memory.turn(session, query, {text: query, timeoutMs: 40});
    return replaceMemoryUpdate(clean, typeof result.context === 'string' ? result.context : '');
  } catch { return clean; }
}
interface VercelFinishReason {unified: string}
interface VercelGenerateResult {content: unknown[]; finishReason: VercelFinishReason}
interface VercelStreamPart {type: string; delta?: unknown; finishReason?: VercelFinishReason}
interface VercelStreamResult<TPart extends VercelStreamPart> {stream: ReadableStream<TPart>}
interface VercelCallOptions {prompt: PromptMessage[]}
interface VercelWrapOptions<TGenerateResult, TStreamResult> {
  doGenerate: () => PromiseLike<TGenerateResult>;
  doStream: () => PromiseLike<TStreamResult>;
  params: VercelCallOptions;
  model: unknown;
}
function captureVercelResponse(memory: MemoryClient, session: string, response: string, succeeded: boolean): void {
  const text = response.trim().slice(0, 20000);
  if (!session.trim() || !text) return;
  try {
    void memory.turn(session, '', {event: 'SessionEnd', budget: 0, timeoutMs: 250,
      payload: {response: text, succeeded}}).catch(() => {});
  } catch { /* Completion capture never changes the model result. */ }
}
/** Current Vercel AI SDK middleware: pass to ai.wrapLanguageModel({model, middleware}). */
export function vercelMemoryMiddleware(memory: MemoryClient, session: string) {
  return {specificationVersion: 'v4' as const,
    transformParams: async <T extends {prompt: RuntimeMessage[]}>({params}: {
      type: 'generate' | 'stream'; params: T;
    }): Promise<T> =>
      ({...params, prompt: (await injectTurn(memory, session, params.prompt)).map(message =>
        typeof message.content === 'string' && message.content.startsWith('<memory_update>') ?
          {...message, content: [{type: 'text', text: message.content}]} : message)}),
    wrapGenerate: async <TResult extends VercelGenerateResult>({doGenerate}:
      VercelWrapOptions<TResult, unknown>): Promise<TResult> => {
      const result = await doGenerate();
      if (result.finishReason.unified !== 'tool-calls') {
        const succeeded = result.finishReason.unified === 'stop';
        captureVercelResponse(memory, session, textContent(result.content), succeeded);
      }
      return result;
    },
    wrapStream: async <TPart extends VercelStreamPart,
      TResult extends VercelStreamResult<TPart>>({doStream}: VercelWrapOptions<unknown, TResult>): Promise<TResult> => {
      const result = await doStream();
      let response = '';
      const stream = result.stream.pipeThrough(new TransformStream<TPart, TPart>({
        transform(part, controller) {
          if (part.type === 'text-delta' && typeof part.delta === 'string' && response.length < 20000) {
            response += part.delta.slice(0, 20000 - response.length);
          } else if (part.type === 'finish' && part.finishReason?.unified !== 'tool-calls') {
            const succeeded = part.finishReason?.unified === 'stop';
            captureVercelResponse(memory, session, response, succeeded);
          }
          controller.enqueue(part);
        },
      }));
      return {...result, stream} as TResult;
    }};
}
export interface MastraRequestContext {get(key: string): unknown}
export interface MastraMessageList {
  clearSystemMessages(tag: string): unknown;
  addSystem(content: string, tag?: string): unknown;
}
export interface MastraMemoryOptions {
  /** Resolve each call's thread/session ID from Mastra's request context. */
  getSessionId?: (requestContext: unknown) => string | undefined;
  /** Resolve request scope (such as `user:alice`) when sharing a client across principals. */
  getContext?: (requestContext: unknown) => string[] | undefined;
}
export interface MastraProcessInputArgs {
  messages: PromptMessage[];
  messageList: MastraMessageList;
  requestContext?: unknown;
}
export interface MastraProcessOutputArgs<TMessage extends PromptMessage = RuntimeMessage> {
  messages: TMessage[];
  requestContext?: unknown;
}

const MASTRA_MEMORY_TAG = 'unimem-memory';
function mastraSession(fallback: string, requestContext: unknown, options: MastraMemoryOptions): string {
  try {
    const value = options.getSessionId ? options.getSessionId(requestContext) : fallback;
    return typeof value === 'string' && value.trim() ? value : '';
  } catch { return ''; }
}
function mastraContext(requestContext: unknown, options: MastraMemoryOptions): string[] | null {
  if (!options.getContext) return [];
  try {
    const value = options.getContext(requestContext);
    if (!Array.isArray(value)) return null;
    const context = value.filter((item): item is string => typeof item === 'string' && !!item.trim());
    return context.length ? context : null;
  } catch { return null; }
}
function finalAssistantText(messages: PromptMessage[]): string {
  const last = messages[messages.length - 1];
  if (!last || last.role !== 'assistant' || isMemoryUpdate(last)) return '';
  return textContent(last.content).trim().slice(0, 20000);
}

/** Mastra input processor; add it to `Agent.inputProcessors`. */
export function mastraMemoryProcessor(memory: MemoryClient, session: string, options: MastraMemoryOptions = {}) {
  return {id: MASTRA_MEMORY_TAG, name: 'UniMem memory',
    processInput: async ({messages, messageList, requestContext}: MastraProcessInputArgs): Promise<any> => {
      try { messageList.clearSystemMessages(MASTRA_MEMORY_TAG); } catch { return messageList; }
      const last = messages[messages.length - 1];
      const query = last?.role === 'user' && !isMemoryUpdate(last) ? textContent(last.content) : '';
      const scopedSession = mastraSession(session, requestContext, options);
      const requestContextScope = mastraContext(requestContext, options);
      if (!query.trim() || !scopedSession || requestContextScope === null) return messageList;
      try {
        const result = await memory.turn(scopedSession, query,
          {text: query, timeoutMs: 40, context: requestContextScope});
        if (typeof result.context === 'string' && result.context.trim()) {
          messageList.addSystem(result.context, MASTRA_MEMORY_TAG);
        }
      } catch { /* Retrieval is fail-open and never blocks the agent run. */ }
      return messageList;
    }};
}

/** Mastra output processor; add it to `Agent.outputProcessors` to capture completion evidence. */
export function mastraMemoryOutputProcessor(memory: MemoryClient, session: string, options: MastraMemoryOptions = {}) {
  return {id: `${MASTRA_MEMORY_TAG}-output`, name: 'UniMem completion capture',
    processOutputResult: async <TMessage extends PromptMessage>({messages, requestContext}:
      MastraProcessOutputArgs<TMessage>): Promise<TMessage[]> => {
      const response = finalAssistantText(messages);
      const scopedSession = mastraSession(session, requestContext, options);
      const requestContextScope = mastraContext(requestContext, options);
      if (!response || !scopedSession || requestContextScope === null) return messages;
      try {
        void memory.turn(scopedSession, '', {event: 'SessionEnd', budget: 0, timeoutMs: 250,
          context: requestContextScope, payload: {response, succeeded: true}}).catch(() => {});
      } catch { /* Capture is fail-open and never replaces the agent response. */ }
      return messages;
    }};
}
