# Competitive analysis: agent-memory frameworks vs. CommonTrace

**Scope.** Six OSS agent-memory projects were cloned in full and read at the
source level — not benchmarked from their docs, not summarized from blog
posts. Every claim below cites a specific file (and, where useful, a
function) in the competitor's own repository, at the commit pinned in the
table below. Where CommonTrace already does the equivalent thing, or has
deliberately chosen not to, that is stated as plainly as the gap itself —
this is not a "why we're better" document, it's a "what exists elsewhere
that this codebase does not, and whether that's an oversight or a choice."

| Project | Repo | Commit pinned | What it optimizes for |
|---|---|---|---|
| **mem0** | `mem0ai/mem0` | `0df3e4b` (2026-09-16) | Fast per-user chat personalization; largest community |
| **Graphiti** (Zep) | `getzep/graphiti` | `c035afb` (2026-09-11) | Bi-temporal knowledge graph; "what was true when" |
| **Letta** (MemGPT) | `letta-ai/letta`, `archive` branch | `56ba9c2` (2026-08-13) | Agent-editable memory blocks; stateful runtime |
| **LangMem** | `langchain-ai/langmem` | `9d033b4` (2026-09-08) | Prompt/behavior optimization from trajectories |
| **Cognee** | `topoteretes/cognee` | `c0d18c8` (2026-09-09) | Multi-agent shared knowledge graph, ingestion pipeline |
| **Memobase** | `memodb-io/memobase` | `358c16b` (2026-01-11) | Structured per-end-user profiles |

CommonTrace occupies a distinct niche from all six: it is not a chat-memory
layer for personalizing conversations with an end user. It is a
**protocol for turning one agent fleet's failures into governed, causally
measured, reusable rules** (`protocol/PROTOCOL.md`) — closer to an
experimentation/observability platform for a coding or support fleet than
to a personal-assistant memory. That framing matters for reading the gaps
below: some of them (Memobase's per-user profiles, LangMem's prompt
rewriting) are close to **out of scope** for what this product claims to
be, and are marked as such rather than as omissions.

---

## 1. What each competitor does, in its own code

### 1.1 mem0 — extract, then ADD/UPDATE/DELETE/NONE

`mem0/configs/prompts.py`'s `FACT_RETRIEVAL_PROMPT` turns a conversation
turn into a list of atomic facts. `DEFAULT_UPDATE_MEMORY_PROMPT` (same
file, ~L176) then does the interesting part: it is handed the new facts
**and** the existing memory closest to them by embedding search, and in one
LLM call decides per fact whether to `ADD` (new), `UPDATE` (same subject,
better/different phrasing — keeps the same row id), `DELETE` (the new fact
contradicts an old one), or `NONE` (already known). This is **write-time
deduplication and contradiction resolution**, done by the model itself
rather than by a fixed similarity threshold.

mem0 also ships a full reranker abstraction (`mem0/reranker/`) —
`CohereReranker`, `SentenceTransformerReranker`, `LLMReranker`,
`ZeroEntropyReranker` — all implementing one `BaseReranker.rerank(query,
documents, top_k)` interface, so a deployment can swap the second-stage
scorer independently of the first-stage retriever.

### 1.2 Graphiti — bi-temporal facts, not documents

Graphiti's central object is an edge with **four** timestamps:
`created_at`/`expired_at` (when the *system* learned/superseded a fact) and
`valid_at`/`invalid_at` (when the fact was *actually true in the world*).
`graphiti_core/search/search_utils.py` implements three rerankers behind one
`SearchConfig` — `rrf` (`search_utils.py:1775`, textbook Reciprocal Rank
Fusion), `mmr` (`maximal_marginal_relevance`, `search_utils.py:1901`,
diversity-aware selection trading relevance against redundancy with a query
vector and a λ parameter), and graph-native ones (`node_distance_reranker`,
`episode_mentions_reranker`) that rank by graph proximity to a center node
or by how many source episodes mention a node. Recipes
(`search_config_recipes.py`) compose these per node/edge/episode/community
type.

Because every edge carries a validity interval, Graphiti can answer "what
did we believe as of March 1st" even after later edges have superseded it —
a real point-in-time query, not just an audit log of changes.

### 1.3 Letta (MemGPT) — the agent edits its own memory, live

`letta/functions/function_sets/base.py` exposes memory management **as
ordinary tool calls the agent invokes mid-conversation**:
`core_memory_append`/`core_memory_replace` (L246, L263) edit a small
always-in-context "core memory" block; `archival_memory_insert`/
`archival_memory_search` (L164, L194) write/read a separate long-term
vector store; `conversation_search` (L87) does hybrid text+semantic search
over raw message history. A fourth tool, `rethink_memory` (L283), lets the
agent **rewrite an entire memory block from scratch**, integrating new
information into the existing text rather than appending to it.

There is no human approval step anywhere in this loop — the agent's own
tool call is the write. Letta separately runs a background "sleep-time"
agent (`letta/services/summarizer/summarizer.py`,
`letta/agents/ephemeral_summary_agent.py`) that periodically compresses
conversation history when the context window fills, independent of the
main agent's turn.

### 1.4 LangMem — optimize the prompt, not a memory store

`src/langmem/prompts/optimization.py`'s `create_prompt_optimizer` takes a
system prompt plus a trajectory (conversation + outcome/feedback) and
returns an **improved prompt string**, via one of three strategies
(`gradient`, `metaprompt`, `prompt_memory` —
`src/langmem/prompts/{gradient,metaprompt,stateless}.py`). This is
structurally different from every other project here: there is no discrete,
inspectable "lesson" object at all. The unit of learning is the whole
prompt, rewritten by an LLM, with no per-instruction attribution and no way
to say "this specific sentence helped, that one didn't."

`src/langmem/knowledge/extraction.py` separately implements a more
conventional memory-store manager (`create_memory_store_manager`, backed
by LangGraph's `BaseStore`), using `trustcall.create_extractor` for
schema-constrained fact extraction.

### 1.5 Cognee — retrieval reweighted by what actually worked

Two mechanisms stood out as neither mem0's nor Graphiti's:

- **`cognee/tasks/memify/apply_frequency_weights.py`**: after a session
  where the agent's answer was judged correct, every graph node/edge that
  was *actually used* to produce that answer gets its usage weight
  incremented (`FREQUENCY_WEIGHT_INCREMENT = 1.0`). This is a direct,
  automatic feedback loop from **outcome → future ranking**.
- **`cognee/modules/truth_subspace/`**: builds a small set of "truth
  centroid" embeddings (`centroids.py`, `DEFAULT_K = 8`) from
  previously-validated "session learnings," then reranks newly retrieved
  content by cosine similarity to those centroids
  (`align.py:node_coords`) — biasing retrieval toward content that
  resembles what has previously proven reliable, computed continuously
  rather than as a one-off report.

Cognee also ships a full LLM-judged evaluation harness
(`cognee/eval_framework/`) — F1, rubric grading, context coverage, exact
match (`evaluation/metrics/*.py`) — with a benchmark runner, a dashboard,
and Modal-based distributed eval (`run_beam_eval.py`, `modal_run_eval.py`).

### 1.6 Memobase — structured per-user profiles, not free-text facts

`src/server/api/memobase_server/controllers/modal/chat/merge.py`'s
`merge_or_valid_new_memos` maps every extracted fact onto a fixed
`(topic, sub_topic)` slot (defined per project in `UserProfileTopic`) and
resolves each slot's content via an LLM call
(`handle_profile_merge_or_valid`) that can add, update, or delete that one
attribute. The result is a **structured, schema'd profile per end user**
(job title, communication preference, etc.), not a growing list of
free-text memories.

---

## 2. Where CommonTrace already stands (so the gaps below are read in context)

This is not a green-field comparison — three of the mechanisms above are
things this codebase built *from* researching these competitors, in the
session immediately before this report, and are already merged to `main`:

- **mem0-style write-time dedup/contradiction handling** →
  `commontrace/redundancy.py` (near-duplicate detection) plus the
  approval-time refusal in `commontrace/commands/lesson_cmd.py:run_approve`
  and MCP's `approve_lesson` (`commontrace/mcp_server.py`). Deliberately
  lexical rather than LLM-judged (see that module's own docstring for why:
  the core install is PyYAML-only), and deliberately at *approval* time
  rather than *every write*, because a scaffolded candidate's body is
  template text until then.
- **Graphiti-style diversity-aware admission** → the redundancy-aware
  injection budget in `commontrace/dosage.py` (`redundancy_threshold`),
  applied as a hard gate in rank order rather than MMR's blended
  re-score — see that module's docstring for why the two are not the same
  thing and why this codebase picked the more auditable one.
- **Cognee-style corpus consolidation without an LLM judge** →
  `commontrace consolidate` (`commontrace/consolidate.py`): fuse, archive,
  and contradiction candidates in one report, reusing the pre-existing
  `commontrace/reliability.py:find_contradictions` rather than
  reinventing it.

What none of the six competitors have, and CommonTrace's whole value
proposition rests on: a **randomized holdout** that measures whether a
memory *causes* a better outcome, not just whether it correlates with one
(`commontrace/holdout_io.py`, `commontrace experiment`, `PILOT.md`). Every
competitor above either has no causal measurement at all, or (Cognee's
`eval_framework`) measures *retrieval/answer quality* against a labelled
benchmark, which is a different question from "did injecting this specific
memory change this specific fleet's specific outcome." That is the one
capability none of them ship, and it is the one this codebase is built
around.

---

## 3. Gaps: concrete, code-referenced, not yet addressed

Ordered by estimated value ÷ effort, highest first.

### 3.1 HIGH — Retrieval ranking never consults measured reliability

**The gap.** `commontrace/retrieval.py`'s `rank_lessons` sorts on
`(relevance, score, importance, uses)` (line ~375) — `uses` (retrieval
count) is a **tie-breaker only**, applied when relevance and raw score are
already exactly equal. `commontrace/reliability.py` computes, for every
lesson with enough evidence, a Wilson-lower-bound precision and a
HELPS/HURTS/MISCALIBRATED verdict — and nothing in the retrieval path ever
reads it. A lesson `commontrace reliability` has flagged as HARMFUL still
ranks purely on lexical/semantic match to the query; a lesson proven
RELIABLE gets no ranking boost over an UNPROVEN one that happens to match
slightly better. Confirmed by grep: `commontrace/retrieval.py` has zero
imports from `commontrace/reliability.py`.

**Reference.** Cognee's `apply_frequency_weights.py` and `truth_subspace/`
(§1.5) both close exactly this loop — outcome feeds back into ranking,
automatically and continuously, not just into a separate report a human
reads later.

**Why this matters more here than for a chat-memory product.** This
product's entire pitch is "does this rule improve outcomes" — serving a
HARMFUL lesson at the same rank as everything else, every time, until a
human notices the reliability report and manually rejects it, is the one
place the causal-measurement story doesn't reach retrieval.

**Suggested shape, keeping the auditability CommonTrace is built on:** an
opt-in `reliability_weight` in `RetrievalConfig` (same
`memory/retrieval.json` file as `redundancy_threshold`), read at query
time from a pre-computed reliability snapshot (not a live statistical
recompute per query — expensive and unnecessary), applied as a small
multiplicative adjustment to `score` for lessons with a HARMFUL or
HARMFUL/RELIABLE verdict — not a silent auto-demotion, and logged in the
holdout record so an experiment can see it happened. Off by default, per
this codebase's own consistent pattern for anything that changes
eligibility.

### 3.2 MEDIUM — No LLM-judged answer-quality benchmark

**The gap.** `commontrace/reference/measure_performance.py` and
`commons/eval/` measure retrieval quality (precision@k, recall@k, MRR,
collateral/pollution ratio) against labelled queries — rigorous, but
entirely lexical/statistical. There is no benchmark that asks "given this
task and the lessons retrieved, does the agent's *actual answer* get
better" — the retrieval-quality metrics are a proxy for that, not a
measurement of it.

**Reference.** `cognee/eval_framework/evaluation/metrics/` (`f1.py`,
`rubric.py`, `context_coverage.py`, `exact_match.py`) plus
`direct_llm_eval_adapter.py`/`deep_eval_adapter.py` — an LLM-judge layer on
top of retrieval, with a runner and dashboard.

**Caveat, stated plainly:** this is real additional value but it is not
free — every LLM-judge metric this codebase has looked at elsewhere
(`hub/SCALING.md`, `STRATEGY.md`'s repeated audits of exactly this kind of
instrument) has needed its own calibration pass before the number it
produces could be trusted, and an LLM judge is a recurring cost per
benchmark run rather than the current benchmark's one-time fixed corpus.
Worth doing, but scope it as its own project with the same
measure-before-you-ship discipline `commontrace/redundancy.py`'s threshold
got, not as a quick add.

### 3.3 MEDIUM — No bi-temporal "what did we believe as of X" query

**The gap.** `commontrace/environments.py` already says this out loud:
> "this module does not attempt point-in-time serving, canary traffic
> splitting, or ring targeting, none of which can be built honestly
> without a real content-addressed revision STORE (keeping the text, not
> just its hash). That is separate, larger work, named here rather than
> implied to exist."

`commontrace/revision.py`/`memory/lesson_revisions.jsonl` record a
before/after **hash** of a lesson's content on every change — enough to
detect that a lesson changed mid-experiment (`integrity.py`'s
`check_treatment_stability`), not enough to answer "what did lesson X
actually say on March 1st," because the prior text itself isn't kept.

**Reference.** Graphiti's edges carry `valid_at`/`invalid_at` and
`created_at`/`expired_at` on the fact itself (§1.2) — a real interval, over
stored text, queryable at any past instant.

**This is explicitly named as future work by the codebase itself**, not a
silent gap — the fix (store the actual prior text, not just its hash, in
the revision journal) is a natural, bounded extension of `lesson_io.py`'s
existing journal, and would upgrade `environments.py`'s "which release is
current" answer into "what did this environment actually serve on any
past date."

### 3.4 MEDIUM — No lesson-refinement suggestion for underperforming lessons

**The gap.** `commontrace reliability` labels a lesson MISCALIBRATED
("fires often, rarely helps → tighten `applies_when`") but stops at the
label. A human must open the file and rewrite it by hand; nothing proposes
*how*.

**Reference.** LangMem's prompt optimizers (§1.4,
`src/langmem/prompts/{gradient,metaprompt}.py`) take exactly this input
shape — text plus a trajectory of outcomes — and produce a rewritten
version.

**Why not just adopt LangMem's approach wholesale:** LangMem optimizes one
undifferentiated prompt blob with no per-instruction attribution or human
gate, which is the opposite of this codebase's approval-gated, one-lesson-
one-causal-effect model. The useful piece to borrow is narrower: an
*optional* `commontrace lesson suggest-revision <slug>` that feeds a
MISCALIBRATED lesson's current `applies_when`/`do_not_apply_when` plus the
evidence (`Evidence` records `reliability.py` already collects — which
occasions it fired on and how those went) to an LLM, and writes the
suggestion as a **draft the existing approval gate still reviews** — not
an auto-applied edit. This preserves every governance property
`commontrace/commands/lesson_cmd.py`'s `run_approve` already enforces
(scaffolding check, content-safety scan, now the redundancy check) while
closing "the report told me what's wrong but not what to do about it."

### 3.5 LOW — No reranker abstraction / pluggable second-stage scorer

**The gap.** `commontrace/retrieval.py` has exactly one lexical scorer
(`idf-v2`, plus the legacy `count-v1` kept for backward compatibility) and
one fusion mode (RRF, `commontrace/retrieval_io.py`). There is no seam for
a deployment to plug in a cross-encoder or an LLM-based reranker as a
second stage over the same candidate set.

**Reference.** mem0's `mem0/reranker/` (§1.1): one `BaseReranker`
interface, four concrete implementations, selected by config.

**Why LOW rather than MEDIUM:** the core install's whole design point is
"works with PyYAML alone, degrades gracefully without the optional
`attention` extra" (`README.md`, `pyproject.toml`'s extras). A reranker
abstraction is legitimate future work for a store that already has the
`attention` extra installed and wants a second-stage boost, but it is
additive to an already-working pipeline, not a hole in it the way §3.1 is.

### 3.6 LOW / SCOPE QUESTION — No per-end-user structured profile tier

**The gap, stated as a question rather than a finding.** Memobase's
`(topic, sub_topic)` profile model (§1.6) answers "what do we know about
*this specific counterparty*" — a support agent's customer, a sales
agent's prospect. CommonTrace's Trace/Lesson model answers "what has this
*fleet* learned about doing the job in general"
(`protocol/PROTOCOL.md`) — there is no first-class notion of a durable,
structured record scoped to one external individual, updated
incrementally as new facts about them arrive.

**Whether this is a gap or a scope boundary:** `STRATEGY.md` is explicit
throughout that this product's moat is fleet-level generalization, not
per-user personalization — the two are genuinely different problems (a
customer profile does not generalize across customers; a lesson is built
to). Recorded here so it's an explicit decision on the record rather than
an unnoticed absence, not recommended as work to pick up.

### 3.7 LOW — No first-class "session" retrieval scope

**The gap.** mem0's `user_id`/`agent_id`/`run_id` (§1.1, confirmed at
`mem0/memory/main.py`'s `delete_all` signature) let a caller scope a query
to one specific run/session, separate from that user's or agent's whole
history. CommonTrace's nearest equivalent, `memory/episodes/` (one file
per `/commontrace` run) plus `occasion_id`, is not exposed as a retrieval
*filter* — `commontrace query`/MCP `retrieve` have no "only what this
specific occasion has seen so far" mode.

**Why LOW:** the reference pipeline (`SKILL.md`) already scopes each run's
lesson injection to Phase 0, and multi-turn-within-one-occasion is a less
common shape for this product's target fleets (code review, support
tickets) than for mem0's target (long-running chat). Worth a line item, not
urgent.

---

## 4. What NOT to build (stated as loudly as the gaps)

- **LangMem-style whole-prompt rewriting** (§1.4). Abandoning discrete,
  attributable lessons for a single optimized blob would throw away the
  one property (per-lesson causal measurement) that differentiates this
  product from every competitor examined here, including LangMem itself.
- **Letta-style unattended agent self-editing of active memory** (§1.3).
  This codebase already tightened exactly this failure mode this session
  — an agent could previously draft *and* approve its own lesson with no
  independent check (`commontrace/approval.py`'s two-person mode exists
  because of it). Letta's `core_memory_append`/`rethink_memory` tools are
  the unattended-self-edit pattern with no approval gate at all; adopting
  it would be a regression, not a feature.
- **A full property graph over lessons** (Graphiti/Cognee's core data
  model). Those projects model *entities and their evolving relationships*
  — a customer, a product, a fact that supersedes another. CommonTrace
  models *rules for how to act* — the contradiction/redundancy signals
  this codebase already has (`reliability.py`, `redundancy.py`) answer the
  questions that matter for that model without needing a graph database
  as a new operational dependency.

---

## 5. Summary table

| Capability | mem0 | Graphiti | Letta | LangMem | Cognee | Memobase | CommonTrace |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Write-time near-dup / contradiction handling | ✅ LLM | — | — | — | — | ✅ LLM | ✅ lexical (this session) |
| Diversity-aware retrieval admission | — | ✅ MMR | — | — | — | — | ✅ hard gate (this session) |
| Corpus consolidation report | — | — | — | — | ✅ `memify` | — | ✅ `consolidate` (this session) |
| Causal (randomized) outcome measurement | ❌ | ❌ | ❌ | ❌ | ❌ (eval only) | ❌ | ✅ (pre-existing) |
| Outcome/reliability feeds back into ranking | — | — | — | — | ✅ | — | ❌ **(§3.1)** |
| LLM-judged answer-quality benchmark | — | — | — | — | ✅ | — | ❌ **(§3.2)** |
| Point-in-time ("what did we believe when") query | — | ✅ | — | — | partial | — | ❌ **(§3.3, self-documented)** |
| Automated rewrite suggestion for a weak rule | — | — | ✅ (unattended) | ✅ (unattended) | — | — | ❌ **(§3.4, gated version proposed)** |
| Pluggable second-stage reranker | ✅ | ✅ (recipes) | — | — | — | — | ❌ **(§3.5)** |
| Per-end-user structured profile | — | — | — | — | — | ✅ | ❌ (§3.6, scope question) |
| Session-scoped retrieval filter | ✅ | — | — | — | — | — | ❌ (§3.7) |

---

## 6. How this was produced

All six repositories were cloned with `git clone --depth 1` and read
directly — module trees, then targeted `grep`/`read` of the files cited
above — not summarized from README claims. Every code citation in this
document was re-verified against the pinned commit at the time of writing;
line numbers are approximate to the nearest declaration where the
competitor's own file has since reformatted. CommonTrace-side claims (the
retrieval sort key, the `environments.py` quote, the `reliability.py`
import graph) were checked against this repository's own source in the
same session, not asserted from memory.
