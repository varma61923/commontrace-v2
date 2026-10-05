# Algorithm audit and implementation priorities

Reviewed on 2026-10-05 against CommonTrace
`8846feaccf94f6ad9dc37787c3c1f8fb63acdcca`. This complements
[competitor-analysis.md](competitor-analysis.md) with concrete algorithms,
failure modes, and engineering decisions. Concurrent implementation work can
change the CommonTrace paths discussed below. This document does not claim that
features or paper results establish superiority over another implementation.

## Pinned sources and licenses

The references were cloned outside the CommonTrace source tree. Repository
licenses were inspected directly. Links below point to the studied revisions,
rather than a moving default branch.

| System | Studied revision | License and source status |
|---|---|---|
| [Mem0](https://github.com/mem0ai/mem0/tree/abb81c88e1f738a8117d8293530fbc31a5ef8fd9) | `abb81c88e1f738a8117d8293530fbc31a5ef8fd9` | Apache-2.0; reference only |
| [Letta redirect](https://github.com/letta-ai/letta/tree/5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a) | `5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a` | Points to maintained Letta Code; not the historical API server |
| [Letta Code](https://github.com/letta-ai/letta-code/tree/f898fda60932b34ddbcfd389ea414515b0a5d272) | `f898fda60932b34ddbcfd389ea414515b0a5d272` | Apache-2.0; reference only |
| [Graphiti](https://github.com/getzep/graphiti/tree/b7fc30f2a1e288266760640164a37bdb7d1d0f28) | `b7fc30f2a1e288266760640164a37bdb7d1d0f28` | Apache-2.0; reference only |
| [Hindsight](https://github.com/vectorize-io/hindsight/tree/f7dd3f4fd7420f7beec60c32c965e5e5cf7be066) | `f7dd3f4fd7420f7beec60c32c965e5e5cf7be066` | MIT; reference only |
| [Honcho](https://github.com/plastic-labs/honcho/tree/8e4df990d974c146a100e96ab4b3957a6591ceab) | `8e4df990d974c146a100e96ab4b3957a6591ceab` | AGPL-3.0; no implementation source copied |
| [Cognee](https://github.com/topoteretes/cognee/tree/b32d8afc59e1064d9291b9828a8a147be9cc8bab) | `b32d8afc59e1064d9291b9828a8a147be9cc8bab` | Apache-2.0; reference only |

No competitor source is incorporated by this audit. Independently implemented
algorithms can still require operational evaluation; license compatibility alone
does not make a wholesale merge useful.

## What the implementations actually do

### Mem0: compact extracted memories and backend search

Evidence:
[memory/main.py](https://github.com/mem0ai/mem0/blob/abb81c88e1f738a8117d8293530fbc31a5ef8fd9/mem0/memory/main.py),
`Memory._add_to_vector_store` and `Memory._search_vector_store`.

The pinned extraction path retrieves ten similar existing memories, includes
ten recent messages, asks for additive structured memories, embeds the new
texts in a batch, and performs hash deduplication before writing. Its search
path overfetches `max(limit * 4, 60)` semantic candidates, performs keyword
search, computes entity boosts, and combines scores. Its candidate construction
starts from semantic results: keyword scores improve that pool, but a lexical
hit absent from the semantic pool does not independently enter it through this
path. Backend choice therefore materially affects recall and sparse support.

CommonTrace should keep its independent lexical candidate arm. Batch encoding,
content-hash reuse, and bounded structured extraction are useful mechanisms
already present. A fixed top-ten similarity neighborhood cannot guarantee that
all conflicting facts about an entity are seen. CommonTrace's indexed
owner/slot history provides a deterministic alternative for facts with known
structure; free-form facts still need better extraction and semantic checks.

The [Mem0 paper](https://arxiv.org/abs/2504.19413), sections 2.1–2.2,
describes extraction followed by ADD/UPDATE/DELETE/NOOP reasoning, plus a graph
variant that invalidates relationships. That paper-era design differs from the
current additive extraction path. Reported 91% lower p95 latency and over 90%
token savings compare with full-context approaches under the authors' setup,
not with this CommonTrace checkout. A graph's roughly 2% overall gain in that
paper does not justify mandatory graph construction for every write.

### Graphiti: multiple graph scopes and distinct time axes

Evidence:
[search/search.py](https://github.com/getzep/graphiti/blob/b7fc30f2a1e288266760640164a37bdb7d1d0f28/graphiti_core/search/search.py),
`search/search_utils.py`, `search/search_filters.py`,
`search/search_config_recipes.py`, and
`utils/maintenance/edge_operations.py::resolve_edge_contradictions`.

Search conditionally embeds a query only when the configured arms need a
vector, runs edge/node/episode/community searches, and supports recipes using
full text, cosine similarity, BFS, RRF, MMR, graph distance, and cross-encoders.
BFS depth and limits are caller controls. Temporal filters distinguish
`valid_at`, `invalid_at`, `created_at`, and `expired_at`. The contradiction
resolver leaves nonoverlapping validity intervals alone; a newer overlapping
fact can close an older fact's validity and record expiration separately.

The [Zep paper](https://arxiv.org/abs/2501.13956), sections 2–3,
explains episode/entity/community layers, the observed versus world-time
distinction, and dynamic community summaries. Community search is a routing
mechanism for broad questions; summaries should lead back to exact evidence.
Community refresh and entity/edge extraction have write cost. CommonTrace's
bounded entity co-occurrence traversal preserves sources, but does not have
typed causal edges or full Graphiti entity resolution.

The important remaining temporal gap is that CommonTrace facts' `at` is
normally an observation timestamp. When a person reports today that they moved
last month, observed time and effective time are different. A full design needs
independent validated validity fields, portable export/import support, and tests
for late reports, retractions, overlapping intervals, unknown dates, and source
deletion. Adding columns without grounding their semantics is insufficient.

### Hindsight: independently retrieved evidence and budgeted refinement

Evidence:
[search/retrieval.py](https://github.com/vectorize-io/hindsight/blob/f7dd3f4fd7420f7beec60c32c965e5e5cf7be066/hindsight-api-slim/hindsight_api/engine/search/retrieval.py),
`search/fusion.py`, `search/reranking.py`, `search/graph_retrieval.py`,
`retain/fact_extraction.py`, `retain/embedding_coalescer.py`,
`retain/memory_budget.py`, and migration
`a4b5c6d7e8f9_fix_per_bank_vector_index_type.py`.

The recall orchestration passes semantic, BM25, graph, and optional temporal
retrieval to its memories store. Fusion preserves each source's ranks and raw
semantic/keyword scores, caps overexpanding arms when configured, and offers
interleaving for deduplication workloads where RRF can bury a semantic-only
match. Reranking and final context budgets are separate stages. Vector indexes
are backend dependent; the inspected migration selects among HNSW, DiskANN,
and vector-chord indexes rather than universally scanning all vectors.

Retain code also addresses operational costs absent from architecture diagrams:
recursive splitting of oversized content, coalescing embedding requests, and a
byte budget for extracted but unwritten state. A message-count batch limit does
not bound bytes when individual messages are large. Those mechanisms are more
directly useful than adding another obligatory model call to every request.

The [Hindsight technical report](https://arxiv.org/abs/2512.12818),
sections 4.2–4.3 and evaluation sections, describes four networks
(world/experience/opinion/observation), decayed graph activation, temporal
interval matching, RRF, cross-encoder refinement, and budgeted selection.
The strongest reported results use much larger answer models than the local
1.5B model available here. The inspected text also leaves retrieval token
budgets as `<add>` placeholders in its evaluation description. Neither feature
presence nor those paper results provide a matched CPU latency baseline.

CommonTrace already uses independent sparse/dense arms and bounded support
chains. Useful next diagnostics are raw per-arm signals and support coverage;
RRF values alone are not calibrated answer probabilities. Adding learned
opinions should wait until the product can distinguish an agent's inference
from a user-confirmed fact and invalidate all dependent conclusions.

### Letta: explicit context paging and reusable memory work

Evidence:
[local/compaction.ts](https://github.com/letta-ai/letta-code/blob/f898fda60932b34ddbcfd389ea414515b0a5d272/src/backend/local/compaction.ts),
`local/initial-memory.ts`, and
`src/skills/builtin/curating-memory-palace/SKILL.md`.

The maintained local implementation plans all-context or sliding-window
compaction, preserves a recent suffix, handles tool-call boundaries, clips
oversized tool returns, and asks summaries to retain identifiers and lookup
hints. Memory files provide explicit curated working context. This is agent
controlled memory management, not a proof that flat automatic recall is weak
for every workload. The default branch redirect should not be represented as
a review of the archived Letta API server.

[MemGPT](https://arxiv.org/abs/2310.08560) explains working context
versus externally retrieved archival/recall storage.
[Sleep-time Compute](https://arxiv.org/abs/2504.13171) explores
amortizing preparation across related future queries; it reports about 5× less
test-time compute at matched accuracy on its stateful reasoning tasks and notes
that query predictability affects usefulness. Deterministic index preparation
and vector reuse fit the amortization principle without speculative inference.
Precomputing answers to an unknowable future question is a different workload.

### Honcho: peer attribution and dependency-aware background work

Evidence:
[architecture documentation](https://github.com/plastic-labs/honcho/blob/8e4df990d974c146a100e96ab4b3957a6591ceab/docs/v3/documentation/core-concepts/architecture.mdx),
`src/utils/search.py`, `src/dialectic/core.py`, and
`src/deriver/scope_backfill.py`.

Peer/session/workspace scopes preserve attribution and visibility. The write
path stores messages and queues reasoning; background derivation,
summarization, and consolidation maintain peer representations. Query-time
dialectic can trace a conclusion to premises. Search supports vector and full
text sources with RRF; pgvector search oversamples to deduplicate messages with
multiple embedding chunks. Scope backfill performs bounded chunks and avoids
holding DB sessions over embedding/vector service calls.

CommonTrace should preserve ownership and premise invalidation while moving
reusable work outside the query path. Its local extraction can likewise page
past a checkpoint without keeping a read transaction open over a model call.
An asynchronous queue additionally needs leases, retries, idempotency, and
observable lag before it is production ready; merely spawning a background
thread is insufficient. No Honcho source was copied. Its linked technical
article returned HTTP 403; no reviewed Honcho research paper is claimed.

### Cognee: graph neighborhood projection and configuration tradeoffs

Evidence:
[brute_force_triplet_search.py](https://github.com/topoteretes/cognee/blob/b32d8afc59e1064d9291b9828a8a147be9cc8bab/cognee/modules/retrieval/utils/brute_force_triplet_search.py),
`node_edge_vector_search.py`, and summary retrievers.

The inspected retrieval path supports vector seed limits, explicit neighborhood
depth, optional whole-graph projection, node/edge distance handling, and scoring
of expansion nodes by vector-store IDs. Neighborhood projection without seed
IDs raises an error; the nonneighborhood path can project a much wider graph.
Its configurable graph strategy should not be interpreted as universally
bounded or inherently efficient at ten-million-token scale.

The [Cognee paper](https://arxiv.org/abs/2505.24478), sections 4–6,
studies configuration search over chunk size, top-k, graph versus text
retrieval, and prompts on multihop QA tasks. It explicitly discusses small
evaluation samples and limitations of exact-match/F1 measures. Configuration
search on held-out queries is a useful idea; optimizing on benchmark answers
and calling the result general memory quality is not.

## CommonTrace gaps, in order of practical benefit

| Gap at audited revision | Concrete change | Expected mechanism | Required evidence and limits |
|---|---|---|---|
| Novel dense queries still scan all eligible vectors | An optional local candidate index or exact partition pruning, followed by fixed-order exact scoring | Reduce scored vectors when the corpus has exploitable structure | Recall@k against complete exact scan, adversarial uniform vectors, filters, updates, rebuild/restart behavior; worst case can remain linear |
| Extraction loads full session before applying checkpoint | Indexed checkpoint-aware pages with a start-of-run high-water mark | Read and allocate only unseen source turns | Same extracted facts/checkpoints, bounded page size, model failure retry, concurrent appends; row limit alone is not a byte bound |
| Unchanged summaries load full transcript before count check | Indexed count/revision precheck before fetching text | Avoid repeated materialization and sentence scoring | Unchanged output, add/delete invalidation, historical/filter exclusions; count must reflect the selected session |
| Deletion repairs all belief chains | Repair only affected owner/slot chains after collecting lost premises | Work scales with affected belief histories | Cross-session premise deletion, predecessor reinstatement, reversions, late arrival; cascade support must be included |
| Confidence considers best lexical overlap from first five used turns | Expose evidence coverage, arm signals, contradictions, and support-group completeness | Make evidence insufficiency observable; later calibration can improve abstention | Disjoint answerable/unanswerable validation, precision/recall and calibration; a heuristic is not a probability |
| Validity mostly follows observed fact time | Independently grounded event/valid time with observed time retained | Correct late-reported and historical knowledge updates | Date extraction validation, unknown dates, overlapping intervals, export/import and deletion semantics |
| Summaries are session-wide 3-sentence extracts | Topic/session routing summaries linked to exact source intervals | Improve broad recall without placing whole history in context | Span coverage at equal tokens, update costs, no raw evidence loss; hierarchy can otherwise hide rare facts |
| Entity links are heuristic co-occurrences | Confirmed aliases and explicitly sourced typed relations | Fewer fragmented entities and more useful multihop paths | Disjoint entity-resolution evaluation, ownership/scope isolation, bounded fanout; no unsupported inferred edges |

The ingestion implementation work in this iteration is addressing checkpoint
paging and localized belief repair. Existing retained improvements and their
measured workloads are in [batched-retrieval.md](batched-retrieval.md) and
[memory-preparation.md](memory-preparation.md). These should remain separately
attributed: repeated-query vector reuse is different from novel-query search,
and a synthetic vector corpus is different from official answer quality.

## What an official comparison measures

The authoritative code is `benchmarks/judges/` and
`benchmarks/conversation_bench.py::summarize`; none is modified by this audit.

* **LoCoMo:** categories 1–4 are multi-hop, temporal, open-domain, and single-hop.
  Category 5 is excluded. The official-style judge emits CORRECT/WRONG; a
  question's score is 1/0 and overall accuracy is the mean over scorable rows.
  Open-domain is the actual category name here, not a separately validated
  "open-domain temporal" category.
* **LongMemEval:** single-session-user, single-session-assistant,
  single-session-preference, multi-session, knowledge-update,
  temporal-reasoning, and abstention use their category-specific prompts.
  The reproduced official parser treats a response containing `yes` as correct.
  Temporal prompts allow the official off-by-one date-difference tolerance;
  knowledge-update prompts allow prior information alongside the correct update.
* **BEAM:** ordinary rubric scores take values 0/0.5/1. Event ordering combines
  normalized Kendall tau-b with event F1; recovering all events and getting
  their ordering correct are distinct requirements. Ability breakdowns use mean
  scores from the corresponding rows. Binary `accuracy` and `mean_score` are
  separate report fields and must not be conflated.
* **Retrieval-only results:** evidence coverage, evidence completeness,
  answer-in-context, session recall, and context tokens are diagnostics. They
  are not a substitute for answer generation and the official judge.

A local answer/judge run can use the unchanged official prompt and parser,
while still differing from a published model configuration. It must disclose
the local model, corpus revision, sample size, random seed, context budget,
retrieval settings, and skipped/failed rows. Published scores from different
papers are not a matched competitor leaderboard. A weaker local judge's labels
also require a checked sample before they can support an accuracy claim.

## Interpreting a 20× objective

Twentyfold improvement is a hypothesis to measure for a particular latency,
throughput, allocation, token, or cost workload. Accuracy is bounded: increasing
a 94.6% score twentyfold is mathematically impossible. A 20× reduction in errors
would instead mean 99.73% accuracy and require sufficiently many held-out
questions to distinguish that error rate reliably.

For end-to-end latency, Amdahl's law matters. Making a retrieval stage 20× faster
cannot make the request 20× faster when local generation dominates; if retrieval
is 10% of the request, the maximum gain from eliminating it is approximately
1.11×. Repeated-query warm caching can produce large gains when encoding
dominates, but does not imply the same gain for unique questions or cold starts.

Accept an optimization when it has an identified bottleneck, a reproducible
before/after workload, relevant quality invariants, bounded resource use, and
clear deployment costs. Preserve useful reference, test, and judge code:
rewriting every line would increase review and regression risk without an
algorithmic reason.
