/** Versioned measured-learning contract, shared by HTTP and MCP clients. */
export const LEARNING_SCHEMA = "unimem.learning.v1" as const;
export const learningOperations = ["capabilities", "configure", "assign", "delivery", "outcome", "run", "status", "rollback"] as const;
export type LearningOperation = typeof learningOperations[number];
export interface LearningReply extends Record<string, unknown> { schema: typeof LEARNING_SCHEMA }
export interface LearningCapabilities extends LearningReply { operations: LearningOperation[] }
export type LearningOutcome = { succeeded: boolean } | { reward: number } | { metrics: Record<string, number> };
export function learningReply(value: unknown): LearningReply {
  if (value && typeof value === "object" && !Array.isArray(value) && (value as Record<string, unknown>).error) {
    const result = value as Record<string, unknown>;
    const error = result.error;
    const nested = error && typeof error === "object" ? error as Record<string, unknown> : undefined;
    const code = nested?.code ?? String(error);
    const detail = nested?.message ?? result.detail ?? code;
    throw new Error(`Learning operation refused (${code}): ${detail}`);
  }
  if (!value || typeof value !== "object" || Array.isArray(value) || (value as Record<string, unknown>).schema !== LEARNING_SCHEMA) {
    throw new Error("Peer does not support unimem.learning.v1");
  }
  return value as LearningReply;
}
export class LearningClient {
  constructor(private readonly transport: (operation: LearningOperation, body: Record<string, unknown>) => Promise<unknown>) {}
  async capabilities(): Promise<LearningCapabilities> {
    const value = learningReply(await this.transport("capabilities", {}));
    if (!Array.isArray(value.operations)) throw new Error("Invalid learning capabilities");
    return value as LearningCapabilities;
  }
  async call(operation: LearningOperation, body: Record<string, unknown> = {}): Promise<LearningReply> {
    if (!learningOperations.includes(operation)) throw new Error("Unknown learning operation");
    if (operation === "capabilities") return this.capabilities();
    const request = structuredClone(body);
    if (!(await this.capabilities()).operations.includes(operation)) throw new Error(`Unsupported learning operation: ${operation}`);
    return learningReply(await this.transport(operation, request));
  }
  configure(mode: "off" | "observe" | "adaptive", options: Record<string, unknown> = {}) { return this.call("configure", {...options, mode}); }
  assign(occasion_id: string, memory_ids: string[], options: {experiment_id?: string; task_context?: string} = {}) {
    return this.call("assign", {...options, occasion_id, memory_ids});
  }
  delivery(occasion_id: string, memory_ids: string[], experiment_id?: string) {
    return this.call("delivery", {occasion_id, memory_ids, ...(experiment_id ? {experiment_id} : {})});
  }
  outcome(occasion_id: string, outcome: LearningOutcome, experiment_id?: string) {
    const keys = Object.keys(outcome);
    if (keys.length !== 1 || !["succeeded", "reward", "metrics"].includes(keys[0]!)) throw new TypeError("Exactly one learning outcome is required");
    if ("succeeded" in outcome && typeof outcome.succeeded !== "boolean") throw new TypeError("succeeded must be boolean");
    if ("reward" in outcome && (!Number.isFinite(outcome.reward) || outcome.reward < 0 || outcome.reward > 1)) throw new RangeError("Invalid learning reward");
    if ("metrics" in outcome && (!outcome.metrics || Object.keys(outcome.metrics).length < 1 || Object.keys(outcome.metrics).length > 64 || Object.values(outcome.metrics).some(value => typeof value !== "number" || !Number.isFinite(value)))) throw new RangeError("Invalid learning metrics");
    return this.call("outcome", {...outcome, occasion_id, ...(experiment_id ? {experiment_id} : {})});
  }
  run(options: Record<string, unknown> = {}) { return this.call("run", options); }
  status(experiment_id?: string) { return this.call("status", experiment_id ? {experiment_id} : {}); }
  rollback(reason: string, options: Record<string, unknown> = {}) { return this.call("rollback", {...options, reason}); }
}
