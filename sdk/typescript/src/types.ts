/**
 * Wire shapes for the Hub's MCP tool surface, mirrored from
 * protocol/schemas/trace.schema.json and hub/server.py's tool
 * signatures -- kept intentionally narrow to the fields this client
 * actually reads or writes, not a full copy of every Hub-internal field.
 */

export interface TraceVote {
  vote_type: "up" | "down";
  feedback_tag?: "" | "outdated" | "wrong" | "security_concern" | "spam";
  feedback_text?: string;
}

export interface TraceRelated {
  relationship?: string;
  trace_id?: string;
}

export interface TraceOutcome {
  resolved?: boolean | null;
  escalated?: boolean | null;
  repeated_error?: boolean | null;
  frustration_signal?: boolean | null;
  tokens_used?: number | null;
  llm_calls?: number | null;
  baseline?: boolean;
}

/** The universal, agent-agnostic unit of captured experience -- the same
 * object shape search_traces/get_trace/contribute_trace all return. */
export interface Trace {
  id: string;
  title: string;
  context_text: string;
  solution_text: string;
  tags: string[];
  agent_type: string;
  agent_id?: string;
  profile?: string;
  extensions?: Record<string, unknown>;
  watch_condition?: string;
  review_after?: string;
  supersedes_trace_id?: string;
  contributor?: string;
  created_at?: string;
  trust?: number;
  retrievals?: number;
  depth?: number;
  votes?: TraceVote[];
  related?: TraceRelated[];
  outcome?: TraceOutcome;
  shared_with_commons?: boolean;
  quarantined?: boolean;
  quarantine_reason?: string;
}

/** Every tool-level failure shares this shape (hub/server.py:_error_response).
 * `error` is a stable machine-readable code ("forbidden", "not_found",
 * "rate_limited", "person_required", ...); everything else is
 * error-specific detail a caller may or may not use. */
export interface ToolErrorBody {
  error: string;
  detail?: string;
  [key: string]: unknown;
}

export interface ContributeTraceArgs {
  title: string;
  context_text: string;
  solution_text: string;
  agent_type: string;
  tags?: string[];
  agent_id?: string;
  profile?: string;
  extensions?: Record<string, unknown>;
  watch_condition?: string;
  review_after?: string;
  outcome?: TraceOutcome;
  idempotency_key?: string;
}

export interface AmendTraceArgs {
  id: string;
  title?: string;
  context_text?: string;
  solution_text?: string;
  tags?: string[];
  outcome?: TraceOutcome;
  idempotency_key?: string;
}

export interface SearchTracesArgs {
  query?: string;
  tags?: string[];
  limit?: number;
  offset?: number;
  occasion_id?: string;
  brief?: boolean;
  pinned?: string[];
}

export interface SearchTracesResult {
  traces: Trace[];
  limit: number;
  offset: number;
  has_more: boolean;
  terms: string[];
  terms_ignored: string[];
  /** Present only when `occasion_id` was passed and an operator has a
   * randomized holdout running for this org -- shape intentionally
   * loose here; see hub/crud.py:holdout_for_results. */
  holdout?: Record<string, unknown>;
}

export interface AccountUsage {
  plan: string;
  period: string;
  /** allowance/remaining are -1 (plans.UNLIMITED) for an unmetered plan. */
  commons_queries: {
    used: number;
    allowance: number;
    remaining: number;
    bonus_from_accepted_submissions: number;
  };
  traces: { used: number; limit: number };
  agents: {
    active: number;
    named: number;
    unattributed_agent_id: string;
    unattributed_traces: number;
    /** True when unattributed_traces > 0 -- `active` is then a floor,
     * not an exact count. See hub/crud.py:agents_under_management. */
    is_floor: boolean;
    window_days: number;
    limit: number;
  };
}
