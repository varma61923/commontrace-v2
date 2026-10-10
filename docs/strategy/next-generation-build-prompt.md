# Build prompt: CommonTrace next generation

Paste everything below the line into a capable coding agent working in this
repository. It is self-contained: it states the goal, what exists, what is
broken (measured), what the competition does (with file:line evidence from
their source), the target design, the acceptance gates and the order of work.

Evidence base (2026-10-10): graphify code graphs of CommonTrace and of eight
competitors at these commits: graphiti a9ef13f, zep cfa2ab2, mem0 b7ad69a,
supermemory 552803c, cognee 0ec7a9f, hindsight 44d5340, EverOS fcd41b5,
letta-code d31b879. All eight are Apache-2.0 or MIT; borrow code with
attribution where it is the best implementation. Competitor citations are
relative to each repository root. Benchmark numbers marked "claimed" come from
a competitor README and were not reproduced.

---

## 0. Mission

You are the lead engineer turning CommonTrace into the category-defining agent
memory system. The product is three things working as one:

1. **Lessons learner.** Every agent run (coding agent, chat agent, robot)
   becomes evidence. CommonTrace distils that evidence into lessons:
   procedures, pitfalls, preferences and rules. It validates each lesson
   causally, versions it, and retires it when it stops helping.
2. **Just-in-time injector.** The right lesson or memory reaches the agent's
   context at the moment it matters (session start, the prompt, before a risky
   tool call, right after a failure, before compaction), within a strict
   token and latency budget, without breaking the prompt cache, and never
   when it would hurt.
3. **mem0-class general memory.** Facts, preferences, profiles, entities,
   relationships and conversations, extracted automatically, deduplicated,
   bi-temporal, scoped per user, agent, session and tenant, and retrieved with
   state-of-the-art hybrid search.

The moat: every competitor stores and retrieves. None of them proves that a
memory or lesson made the agent better. CommonTrace already has the
statistics: AIPW estimation, randomized holdouts, anytime-valid confidence
sequences, era schedules and lift certificates. The job is to put that proof
on top of best-in-class memory, learning and injection, and to make it fast,
simple and automatic.

## 1. Ground rules

- Follow `AGENTS.md`: Python 3.10+, stdlib-first, argparse, `os.path.join`, no
  hardcoded platform paths, auto-detected root, no `git stash`, `clean` or
  `restore` in sub-agent briefs, never commit `memory/attention/index.npz`.
- Keep the CommonTrace Protocol (`protocol/PROTOCOL.md`, the JSON Schemas and
  the conformance suite) backward compatible. New behaviour gets new protocol
  sections and new conformance vectors.
- Never claim an unmeasured result. Every phase ends with a before/after
  measurement, committed with the command that produced it. Keep "claimed by
  competitor" and "measured here" visibly separate in all docs.
- Keep local-first parity: everything works offline with local models, and
  the hosted tier is the same API.
- Keep CI green: tests, ruff, strict mypy list, Bandit, pip-audit, the OpenAPI
  and docs drift checks, the conversation-quality regression gate, and the Hub
  suite run from the repository root.
- Prefer deleting and merging code over adding parallel paths (see F7).

## 2. Strengths to preserve

These are real and mostly unmatched. Do not regress them.

- Causal measurement of lessons: holdouts, AIPW, anytime-valid confidence
  sequences, adaptive era-stratified allocation, subgroup effects and
  interaction effects. No competitor measures outcomes.
- Lesson governance: review status, validation, journaled edits, and a
  marketplace with lift certificates.
- Bi-temporal facts: valid time and transaction time (`known_at`), write-time
  contradiction detection (`fact_conflicts.py`), supersession through a
  temporal guard, and provenance lineage (`provenance.lineage`).
- An open protocol, JSON Schemas, a conformance suite, and typed OpenAPI for
  every HTTP API (gateway: 61 operations; Hub: 106).
- Security work competitors lack: PoisonBench, GateMem-style multi-principal
  governance, OAuth 2.1 resource server, Hub row-level security, and an
  offline mode.
- A reproducible benchmark harness with signed manifests and a CI regression
  gate on a pinned LoCoMo sample.

## 3. Flaws to fix (measured, ranked)

**F1 — Critical: fact writes are O(N).** One `hierarchical.add_fact` takes:

| Store size | Time per write |
|---|---|
| 1,000 facts | 137 ms |
| 10,000 facts | 2.35 s |
| 30,000 facts | 5.75 s |

Profile at 10,000 facts: 0.56 s re-parsing the whole `facts.jsonl`
(`load_facts`), 1.06 s rebuilding the whole in-memory dedup and contradiction
index (`_StatementIndex.__init__`, including `fact_conflicts.slot_key` for
every fact), and 0.53 s rewriting the whole file (`save_facts`). Search is
fast (1 ms) because of `fact_index`. At a million facts a single write would
take minutes. mem0, hindsight and cognee write to databases.

**F2 — Critical: no automatic injection.** The repository has no
SessionStart, UserPromptSubmit, PreToolUse, PostToolUseFailure or PreCompact
hooks. Memory reaches an agent only when the agent pulls it (MCP tools,
gateway, SDK, or the `/commontrace` skill's startup phase). By contrast:

- mem0 ships hooks for Claude Code, Cursor, Codex and seven or more other
  agents, all generated from one core (`integrations/claude-code-plugin/hooks/hooks.json:4-108`).
- hindsight ships hooks for Cursor, Cline, Kimi and others (`hindsight-integrations/coding-agents/src`).
- EverOS injects on UserPromptSubmit (`use-cases/claude-code-plugin/hooks/scripts/inject-memories.js:26-28`).
- Zep injects every turn through each framework's native hooks.

**F3 — High: the default retrieval is lexical and the quality evidence is
thin.** Dense search, rerankers and hybrid fact search are all opt-in.

Measured here:
- Evidence-in-context, keyword arm with adaptive budget: LoCoMo 80.29%,
  LongMemEval-S 83.02%, BEAM 100K 79.37%. These are retrieval metrics, not
  answer accuracy.
- Model-judged answer accuracy: 68.5% [61.8, 74.5] on a 200-question LoCoMo
  subset (Gemma 4 31B reader and judge, lexical memory, 1,500 tokens).
- The new rerankers, the hybrid facts channel and the multi-channel adaptive
  budget have never been benchmarked.

The bar:
- Zep committed LoCoMo 0.801 with gpt-4o-mini at about 1,378 context tokens,
  retrieval p50 0.24 s (`zep/benchmarks/locomo/experiments/experiment_20251203_171523/experiment_summary.json`).
- EverOS claims LoCoMo 94.42 and LongMemEval 94.00 (`EverOS/benchmarks/README.md:98-117`).
- mem0's platform claims LoCoMo 92.5 and LongMemEval 94.4 (`mem0/README.md:46-53`).
- hindsight runs a live public leaderboard with per-model accuracy, latency
  and cost (`hindsight/README.md:44-48`).

**F4 — High: the lessons learner is thinner than its competitors'.** It is
missing:
- Session distillation with a curator and an accept/reject writer that
  checks prior lessons. cognee does this, with a watermark that never seals
  entries behind failed calls (`cognee/cognee/modules/session_distillation/distill.py`).
- Case → cluster → skill consolidation with lineage. EverOS keeps
  `source_case_ids` and bounds each prompt to 10 skills and 9 supporting cases
  (`EverOS/src/everos/memory/strategies/extract_agent_skill.py:104-121,190-218`).
- A typed lesson lifecycle (update, extend, deprecate, split, create, none,
  biased toward none) and an incremental reflection cursor. letta-code has
  both (`letta-code/src/agent/subagents/builtin/reflection-v2.md:110-119`;
  `src/cli/helpers/reflection-transcript.ts:30-38`).
- Cold-start import of existing Claude Code and Codex histories, with claim,
  evidence and citation records, authorship checks and a coverage ledger
  (`letta-code/skills/builtin/initializing-memory/SKILL.md:86-176`).
- A cold-start path for validation. Causal lift needs many occasions, so new
  lessons have no quarantine, shadow mode or prior to protect agents in the
  meantime.

**F5 — High: memory extraction and dedup are below the state of the art.**
Fact extraction lacks mem0's rules (`mem0/mem0/configs/prompts.py:468-944`):
- ground relative dates to the observation date;
- record transitions ("switched from X to Y");
- attribute assistant content to the assistant;
- skip echoes.

The write-time contradiction detector I built is lexical: it catches a
negation flip or a single changed number. Paraphrased contradictions need the
optional LLM judge.

Entity resolution lacks graphiti's tiers (`graphiti/graphiti_core/utils/maintenance/dedup_helpers.py:31-37,321-397`;
`graphiti_core/utils/maintenance/edge_operations.py:558,715-783`; `graphiti_core/prompts/dedupe_edges.py:44-118`):
- exact match first;
- then MinHash with an entropy gate;
- then an LLM call for the remainder only;
- one joint duplicate-and-contradiction judgment that expires facts rather
  than deleting them.

**F6 — Medium: no profile tier.** supermemory returns static, dynamic and
custom profile buckets together with search in one call (about 50 ms
claimed). cognee keeps preference weights with decay
(`cognee/cognee/modules/user_preferences/weights.py`). CommonTrace has memory
blocks, but no automatically maintained profile.

**F7 — Medium: surface-area sprawl.** CommonTrace has:
- 306 Python modules and 81,018 lines;
- 69 top-level CLI commands;
- 153 documented environment variables;
- 61 gateway operations.

Several subsystems overlap:
- dedup lives in `hierarchical`, `fact_consolidation` and `consolidate`;
- reranking in `rerank_arm`, `reranking` and the conversation reranker;
- recall in `recall`, `search_recipes` and `conversation.search`.

First value takes too many steps. A call-graph audit found code reachable
only from tests. The Neo4j/FalkorDB mirror was one such case until it was
wired to `commontrace graph mirror` and `graph query --backend mirror`.

**F8 — Medium: new graph features are untuned.** Entity-resolution and
classification thresholds are reasoned guesses, not tuned on data. The
pattern relation extractor favours precision and has low recall. None of it
has a precision/recall benchmark.

**F9 — Medium: cost and operations controls are partial.** Missing:
per-prompt model routing, an LLM response cache and per-prompt token
accounting (graphiti has all three: `graphiti_core/llm_client/prompt_config.py:208`,
`llm_client/cache.py:27`, `llm_client/token_tracker.py:55`); a durable worker with a dead-letter state (EverOS
`src/everos/infra/ome/triggers.py`); and retrieval-quality telemetry per query (EverOS
logs `recall_top_score` and `recall_hit`).

**F10 — Low: CodeQL alerts block pull request #107.** Both are false
positives:
- `hub/auth.py:54` HMAC-SHA256s high-entropy API keys with a secret pepper;
  that is correct.
- `commontrace/ui/app.js:1265` is a UI gate; the gateway enforces 401 on
  every route.

Dismiss them with reasons, and add a CodeQL config comment documenting why.

## 4. What to take from each competitor

| Competitor | Best ideas to adopt | Their gaps (your opening) |
|---|---|---|
| graphiti | Expire facts instead of deleting them, with episode provenance. Tiered dedup (exact → MinHash with entropy gate → LLM). Joint duplicate/contradiction call. Datetime-rule prompts. Method × reranker search recipes with BFS seeded from the first pass. Small-model routing, LLM cache, token tracker. | No learning loop. 5–10+ LLM calls per episode, processed one at a time per group. Expired facts returned by default. No recency. Brute-force vector scan. RRF k=1. No auth. |
| zep | Native-hook injection that saves and retrieves in one round trip. A context template language (`%{edges limit=4 types=[...]}`). Context injected at the end so the prompt cache survives. A context-completeness judge. Pin-or-expose tool parameters. | Closed SaaS engine. No read-after-write. Weak alias merging. LoCoMo 0.80 is beatable. |
| mem0 | Hybrid semantic + BM25 + entity fusion with an explainable score. Extraction prompt rules. A coding-plugin capture spool: local SQLite, detached flush, crash recovery. Inject once per session, skip already-injected, inherit into subagents. Repo-hashed team lane and personal lane. One plugin core with conformance tests. | OSS cannot handle updates or contradictions (ADD-only, links dropped). BM25 cannot surface new candidates. Recall only at the first prompt. Telemetry on by default. Graph, temporal and profiles need the platform. |
| supermemory | Versioned memories with `updates`, `extends` and `derives` relations, `isLatest` and a parent/root chain. Inferred facts down-weighted until reviewed. Static/dynamic profile returned with search. A read-only context block replaced each turn. `forgetAfter` with a reason. | Closed engine. No learning from trajectories. Saves and injects everything by default. Benchmark claims unverifiable. |
| cognee | Session distillation (curate → accept/reject against prior lessons → persist) with a safe watermark. Proposal-first skill improvement. Preference weights with decay. A truth-subspace reranker built from session learnings. Over 20 search types, including coding rules and skills. | Lessons are never validated against outcomes. Heavy pipeline. |
| hindsight | World/experience facts consolidated into observations under explicit rules (prefer update, one facet per observation, cascade state changes, never compute numbers, preserve history) (`engine/consolidation/prompts.py:39-55`). Mental models that refresh and retract when their grounding facts are withdrawn (`engine/reflect/retractions.py`). Priority-ordered directives injected as hard rules (`engine/directives/models.py`). Causal links. RRF k=60 with strategy boosts. A live public benchmark site. | Postgres-heavy setup. No outcome-validated learning. |
| EverOS | LLM boundary detection that segments into MemCells. A case → cluster → skill pipeline with lineage. LLM_MULTIROUND retrieval: an RRF block per sub-query, core-first assembly, at most 3 gap-filling rounds, traces kept as an RL corpus. Markdown as source of truth with sha256 incremental re-embedding. Returning the unextracted buffer at read time. Durable event engine. | Skills never checked against outcomes. "Retire" and maturity unimplemented. No fact contradiction handling or validity windows. No auth. |
| letta-code | Memory as git: reflection in worktrees, commit-trailer provenance. A reflection prompt with an explicit priority order and lifecycle operations. The prompt compiled from committed HEAD, with `<memory_update>` deltas that keep the cache. Core vs deferred memory tiers with size caps enforced at commit. Cold-start import of other agents' histories. A reflection arena (blind A/B of reflection models). | No semantic store. Recall is a linear BM25 scan. Lessons never validated. Always-on core memory bloats the prompt. |

## 5. Target design

### A. Storage engine (fixes F1)

Replace whole-file JSONL mutation with:
- an append-only event log as the source of truth;
- materialised indexes as rebuildable derivatives.

Local tier: SQLite (WAL) with FTS5 for BM25 and a vector index (sqlite-vec or
the existing snapshot store). Server tier: Postgres with pgvector (HNSW) and a
full-text index; reuse the Hub's row-level security for tenancy.

The dedup and contradiction index (`_StatementIndex`) becomes persistent and
incremental, updated per write, never rebuilt. Keep JSONL/markdown export and
import for portability and human editing, as EverOS and letta do with their
files of record.

Gates:

| Metric | Target |
|---|---|
| Single write p95, 1M facts | < 20 ms |
| Batched ingest | ≥ 2,000 facts/s |
| Hybrid search p95, 1M facts | < 50 ms local, < 150 ms server |
| Restart cost | zero re-embedding (vectors cached by content hash) |

Provide a migration from existing stores with a checksum report.

### B. One memory model

All of these share one bi-temporal, versioned record envelope:

- **Episodes**: raw turns, traces and documents.
- **Facts**: subject–relation–object edges with valid/invalid/expired
  times.
- **Entities**: typed by the ontology.
- **Observations and profiles**: consolidated per entity/facet; static and
  dynamic buckets.
- **Lessons**: procedural memory with `applies_when`, `do_not_apply_when` and
  a trigger signature.
- **Directives**: hard rules with priority.

The envelope carries: `id`, `version`, `is_latest`, `relations` (updates,
extends, derives, supersedes, caused_by), `provenance` (source episodes and
traces), `scope` (tenant, user, agent, session, repo), `status` (proposed,
quarantined, shadow, active, deprecated, retired), `valid_from/valid_until`,
`created_at/retracted_at`, `confidence` and `evidence`.

Expired and non-latest records are excluded from retrieval by default.

### C. Write path (fixes F5)

- An extraction prompt that adopts mem0's rules:
  - ground dates to the observation date;
  - record transitions;
  - attribute speakers and the assistant;
  - no echoes or meta;
  - 15–80-word contextual memories;
  - plus graphiti's exclusions (no pronouns or abstractions;
    possessor-qualified kin).
- Entity resolution in tiers: exact → MinHash with an entropy gate → embedding
  candidates (top 15, cosine ≥ 0.6) → one batched LLM call for the remainder.
- A joint duplicate-and-contradiction judgment for facts, as graphiti does.
  Contradiction expires the older fact through the existing temporal guard.
  Keep the lexical detector as the zero-cost first tier.
- Consolidation into observations using hindsight's rules. Mental models
  retract when their grounding facts are retracted.
- Cost:
  - per-prompt model routing (small model for dedup and timestamps);
  - an LLM response cache keyed by prompt hash;
  - a token tracker per prompt;
  - LLM-free fast paths (verbatim match, short summaries appended without a
    call);
  - batching.
- Read-after-write: a search returns the session's unextracted buffer, as
  EverOS does.

### D. Lessons learner (the moat; fixes F4)

1. **Capture.** A durable local spool, as mem0's plugin core has: SQLite,
   detached flush, crash recovery. Record prompts, tool calls, errors, diffs,
   test results and final outcomes for every run.
2. **Segment.** LLM boundary detection into task episodes, as EverOS does,
   with hard token and message caps.
3. **Extract cases.** Record `{task_intent, approach, outcome signal, key
   insight, failure mode, fix}`. Return nothing for thin trajectories.
4. **Distil.** A curator proposes lessons and a writer accepts or rejects each
   one against prior lessons (the cognee flow). Watermark semantics never seal
   un-distilled work.
5. **Consolidate.** Cluster cases by intent. Apply letta's lifecycle
   operations (create, update, extend, deprecate, split, none), at most one per
   run and biased toward none. Bound every prompt: at most 10 lessons and 9
   supporting cases. Keep lineage (`source_case_ids` and trace ids) and
   commit-trailer-style provenance.
6. **Quarantine → shadow → active.** A new lesson starts quarantined. In
   shadow mode, compute it as if injected and log the counterfactual without
   showing it. Promote it on the existing causal machinery: holdout, AIPW,
   anytime-valid confidence sequences, plus a Bayesian prior from offline
   replay for the cold start. Retire it automatically when the lower bound
   turns harmful. Measure time-to-retire.
7. **Cold start.** Import Claude Code, Codex, Cursor and Gemini CLI session
   histories. Record claims with evidence and citations, check who authored a
   quote, keep a coverage ledger, and verify claims against the current code
   before seeding lessons.
8. **Reflection.** An incremental cursor per conversation (reflected-through
   message id). A pre-selector that prefers corrections, repeated preferences
   and failures. A reflection arena for blind A/B of extractor models, feeding
   DPO export.
9. **Typed lesson kinds.** Procedure, pitfall (with error signature),
   preference, directive and tool-usage rule, each with a matching injection
   trigger.

### E. Just-in-time injector (fixes F2)

**Hook kit.** One core, generated per agent, with conformance tests, as mem0's
`agent-plugin-core` does. Targets: Claude Code, Codex, Cursor, Cline, Gemini
CLI, Kimi, Windsurf and OpenHands. Events and what each injects:

| Event | Injects |
|---|---|
| SessionStart | profile, directives, top lessons for this repository |
| UserPromptSubmit | relevance-gated lessons and memories |
| PreToolUse | lessons whose trigger matches the tool, its arguments and file paths; warn before known-bad operations |
| PostToolUseFailure | lessons whose error signature matches the failure; the highest-value moment, and no competitor does it |
| PreCompact | carry critical lessons through compaction |
| SubagentStart | inherit the parent's injected set |
| Stop / SessionEnd | capture and outcome |

**Framework middleware.** LangGraph pre-model hook, ADK `process_llm_request`,
Vercel AI middleware, Pydantic-AI history processor, MS Agent Framework
`before_run`/`after_run`, Mastra, CrewAI and the OpenAI Agents SDK. Save and
retrieve in one round trip, as Zep does.

**Relevance gating.**
- Require the BM25 and dense channels to agree (letta's bootstrap gate: RRF ≥
  0.012 and ≥ 93% of the top score).
- Apply relative score floors.
- Inject once per session and skip already-injected items.
- Never inject quarantined lessons outside shadow mode.

**Budget and placement.**
- A budgeted context template language (Zep-style slots with limits).
- A read-only delimited block with escaped content, replaced each turn
  (supermemory).
- Placement that keeps the prompt cache: a stable core block plus trailing
  `<memory_update>` deltas, as letta and Zep do.

**Latency.** Hook p95 < 50 ms with a local index. Time out to "no injection",
never to an error.

**Safety.** Secret and PII redaction on capture and again on inject. Injected
memory is framed as data, not instructions. PoisonBench runs in CI against the
injector.

### F. Retrieval (fixes F3)

- Hybrid search on by default:
  - BM25 that can add candidates, not just re-rank (mem0's flaw);
  - dense search;
  - entity boost;
  - graph BFS seeded from first-pass hits;
  - temporal filters;
  - fused with RRF k=60;
  - a cross-encoder on the head;
  - an explainable score breakdown.
- Recency decay for episodic memory. For lessons, weight by outcome
  (validated lift).
- For hard queries: query analysis, then agentic multi-round retrieval
  (EverOS LLM_MULTIROUND). Keep the traces as a training corpus.
- Ship a strong local default (e.g. arctic-m or bge-small plus
  bge-reranker-v2-m3) so first-run quality is not lexical-only, with an
  automatic fallback when models are unavailable.

### G. Profiles and preferences (fixes F6)

Automatically maintained static and dynamic profile buckets per user and per
agent, with preference weights that decay. `profile+search` in one call.
Always-inject slots for directives and critical preferences.

### H. Governance

- Tenant, user, agent, session and repo scopes enforced server-side, with
  pin-or-expose tool parameters.
- Hard delete that cascades to derived records and vectors, with an audit
  trail.
- `forget_after` with a reason.
- Telemetry off by default.
- PII and secret redaction.
- Memory-poisoning defenses: quarantine plus provenance plus PoisonBench.

### I. Platform and developer experience (fixes F7)

- `commontrace up`: zero-config first value in under two minutes. It detects
  installed agents, installs hooks, imports history and shows the first
  injected lesson.
- One config file with profiles (local, team, server) replacing most
  environment variables. Target: fewer than 40 documented variables.
- Merge the duplicated subsystems: one dedup pipeline, one reranking stage,
  one recall entry point with recipes. Remove code reachable only from tests;
  run the graphify call-graph audit in CI to prevent regressions.
- A hosted control plane identical to the local API, plus a public
  leaderboard site in the style of hindsight's.
- Use `scripts/export_openapi.py` and the existing OpenAPI contracts to keep
  every SDK in lockstep.

## 6. Benchmarks and acceptance gates

Publish all results with `reproduce.sh`, signed manifests and the named reader
and judge models. Report retrieval metrics and answer accuracy separately.

| Gate | Target |
|---|---|
| LoCoMo answer accuracy (≤ 1,500 context tokens, gpt-4o-mini-class reader and judge) | ≥ 0.85, beating Zep's committed 0.801 at about 1,378 tokens |
| LongMemEval-S answer accuracy | ≥ 0.90 |
| BEAM 100K/1M | report and improve every release |
| Context completeness (Zep's COMPLETE/PARTIAL/INSUFFICIENT judge) | report per category |
| Retrieval latency | p50 < 100 ms, p95 < 250 ms server; p95 < 50 ms local hooks |
| Write path at 1M facts | p95 < 20 ms single; ≥ 2,000 facts/s batched |
| CodingLessonBench (new, open) | task success with vs without lessons on SWE-style tasks, with randomized holdouts: absolute uplift ≥ +5 pp with the lower confidence bound > 0; harmful lessons retired within N occasions; injection precision ≥ 0.8 |
| Injection safety | PoisonBench: zero successful injections of quarantined or poisoned lessons |
| Cost | tokens and LLM calls per ingested episode, reported per release, decreasing |

## 7. Order of work

Each phase ends green in CI, with measurements committed.

1. **P0 Foundations.** Storage engine and migration (A). Unified record
   envelope (B). Dismiss the CodeQL false positives. Gates: F1 numbers.
2. **P1 Injector.** Hook kit for Claude Code, Codex and Cursor first (E),
   then the capture spool (D1). Framework middleware for LangGraph, ADK and
   Vercel. Gates: hook latency, injection precision on a labelled set.
3. **P2 Lessons learner.** Segment, cases, distil, consolidate with
   lifecycle, quarantine and shadow, causal promotion and retirement, history
   import (D). Gates: CodingLessonBench v1.
4. **P3 Memory quality.** Extraction prompt, tiered dedup, joint
   contradiction, observations and mental models, profiles, hybrid-by-default
   retrieval, multi-round retrieval (C, F, G). Gates: LoCoMo and LongMemEval
   targets.
5. **P4 Simplification and platform.** Merge duplicated subsystems,
   consolidate config, `commontrace up`, hosted control plane, public
   leaderboard (I). Gates: module and variable counts, time to first value.

For each phase:
1. Write a short design note in `docs/strategy/`.
2. Write the tests first.
3. Implement.
4. Measure against the previous release.
5. Update `implementation-status.md`, `scorecard.md` and `CHANGELOG.md` with
   measured numbers only.

Stop and report when a gate is missed, rather than lowering it.
