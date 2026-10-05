# Memory architecture research and implementation decisions

Studied on 2026-10-05. The repository revisions below pin the implementation
observations; paper results are authors' reports, not measurements of this
checkout. No competitor implementation source was copied. CommonTrace's official
judges remain unchanged. Comparable answer-quality evaluations have not been run
with a generation model, so outperforming these systems is not established.

## Reference checkouts and licensing

Checkouts live outside CommonTrace's source tree in the research workspace.

| System | Official repository | Studied commit | License inspected |
|---|---|---|---|
| Mem0 | https://github.com/mem0ai/mem0 | `abb81c88e1f738a8117d8293530fbc31a5ef8fd9` | Apache-2.0 |
| Letta | https://github.com/letta-ai/letta | `5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a` | Current repository redirects to Letta Code |
| Letta Code | https://github.com/letta-ai/letta-code | `f898fda60932b34ddbcfd389ea414515b0a5d272` | Apache-2.0 |
| Graphiti | https://github.com/getzep/graphiti | `b7fc30f2a1e288266760640164a37bdb7d1d0f28` | Apache-2.0 |
| Hindsight | https://github.com/vectorize-io/hindsight | `f7dd3f4fd7420f7beec60c32c965e5e5cf7be066` | MIT |
| Honcho | https://github.com/plastic-labs/honcho | `8e4df990d974c146a100e96ab4b3957a6591ceab` | AGPL-3.0 |
| Cognee | https://github.com/topoteretes/cognee | `b32d8afc59e1064d9291b9828a8a147be9cc8bab` | Apache-2.0 |

The currently maintained Letta implementation is `letta-code`; the old API
server resides on Letta's `archive` branch. The redirected default branch is not
silently treated as the historical server implementation. Honcho's AGPL code is
reference material only; the entity traversal here is independently implemented
using CommonTrace's own SQLite schema. Research ideas do not justify copying
licensed implementation code without its required notices.

## Papers and technical sources

* Mem0: [Building Production-Ready AI Agents with Scalable Long-Term Memory](https://arxiv.org/abs/2504.19413).
  Read extraction/update and graph construction sections and experimental
  comparisons. Compare newly extracted facts with existing memories; maintain
  update operations rather than treating every mention as an independent fact.
  Its graph variant's modest overall gain comes with additional processing.
* Zep: [A Temporal Knowledge Graph Architecture for Agent Memory](https://arxiv.org/abs/2501.13956).
  Read episode, semantic/community graph, search and temporal invalidation
  sections. Separate time when an assertion was observed from when it was true;
  preserve episodes supporting edges. Historical knowledge cannot be recovered
  by deleting superseded assertions.
* Letta: [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560)
  and [Sleep-time Compute: Beyond Inference Scaling at Test-time](https://arxiv.org/abs/2504.13171).
  Read context paging/archival recall and query predictability/amortization
  sections. Expensive reusable work belongs off the query path; speculative
  reasoning is advantageous only when future questions are predictable enough.
* Hindsight: [Building Agent Memory that Retains, Recalls, and Reflects](https://arxiv.org/abs/2512.12818).
  Read retain/recall/reflect, entity linkage, four retrieval arms, fusion,
  reranking and budget selection sections. Graph evidence can reveal passages
  that match an intermediate entity rather than the original question. Learned
  opinions and observations should remain distinguishable from source facts.
* Cognee: [Optimizing the Interface Between Knowledge Graphs and LLMs for Complex Reasoning](https://arxiv.org/abs/2505.24478).
  Read modular construction/retrieval and optimization sections. A graph is
  useful when bounded relevant subgraphs expose actual reasoning dependencies;
  construction, chunking and prompt choices influence results substantially.
* Honcho: repository `docs/v3/documentation/core-concepts/architecture.mdx`,
  representation and dialectic implementation, and linked
  [evaluation page](https://honcho.dev/evals/) / [technical article](https://blog.plasticlabs.ai/research/Benchmarking-Honcho).
  The linked article returned HTTP 403 in this environment. No peer-reviewed
  Honcho paper was identified in the inspected sources; this is not represented
  as a paper review. The repository documents asynchronous peer representations,
  scoped sessions, and tracing inferred conclusions back to premises.

PDFs and extracted text were read in the external research workspace. References
are links, not redistributed papers. Current source can differ from paper-era
implementations; those differences matter when making performance claims.

## Implementation evidence and tradeoffs

| System | Inspected implementation | Strength | Cost or limitation |
|---|---|---|---|
| Mem0 | `mem0/memory/main.py::_add_to_vector_store`, graph memory and extraction prompts | Existing-memory retrieval, scoped metadata, batched structured extraction, explicit update operations | Inference-dependent writes; nearest existing-memory limits can miss contradictions; graph adds cost |
| Letta Code | `src/backend/local/initial-memory.ts`, `compaction.ts`, `src/skills/builtin/curating-memory-palace/SKILL.md`, memory task lifecycle | Explicit memory files and bounded working context; reusable memory maintenance | Tool-driven paging requires agent decisions; summaries can omit exact evidence |
| Graphiti | `graphiti_core/search/search.py`, `search_config_recipes.py`, `search_filters.py`, search utilities | Episode/node/edge/community arms; RRF/MMR/BFS/reranker recipes; separate validity/creation/expiration filters | Extraction/entity resolution and graph infrastructure are expensive; broad traversals need bounds |
| Hindsight | `hindsight-api-slim/hindsight_api/engine/search/{retrieval,graph_retrieval,fusion}.py`, retain/consolidation code | Entity/temporal graph discovery; recall budgets; distinct fact/observation layers | Multiple retrieval arms and cross-encoder cost; CPU temporal analysis must not block the event loop |
| Honcho | `src/utils/search.py`, `src/dialectic/{core,prompts}.py`, `src/deriver/scope_backfill.py`, architecture docs | Peer attribution, evidence/premise tracing, scopes, asynchronously maintained representations | Query-time reasoning can add model calls; inference deletion needs dependency tracking; AGPL restrictions |
| Cognee | `cognee/modules/retrieval/utils/brute_force_triplet_search.py`, summary retrievers | Configurable seed count/depth and database neighborhood projection; graph plus source chunks | Unbounded fallback graph retrieval is expensive; learned graph quality and pipeline tuning determine usefulness |

Searches also covered each repository's benchmark/evaluation paths. They use
varying datasets, models, prompts and judges. Their published numbers cannot be
combined into one directly comparable leaderboard for this checkout.

## Capability matrix

`Partial` means a bounded or narrower implementation, not equivalent capability.
`Unverified` means no comparable measurement was performed here. Feature presence
is architectural evidence, not evidence of better accuracy.

| Capability | CommonTrace | Mem0 | Letta Code | Graphiti | Hindsight | Honcho | Cognee |
|---|---|---|---|---|---|---|---|
| Dense retrieval | Optional local embeddings | Yes | Archival/tool retrieval | Yes | Yes | Yes | Yes |
| Sparse retrieval | SQLite FTS5 BM25 | Backend dependent | File/grep tools | Full-text | BM25 | Text search | Retriever dependent |
| Hybrid retrieval | Per-facet RRF | Configuration dependent | Tool composition | Yes | Four arms | Search plus dialectic | Yes |
| Knowledge graph | Entity co-occurrence plus protocol graph | Optional | Memory-file relationships | Yes | Typed memory links | Reasoning dependencies | Yes |
| Temporal graph | Partial: belief chains and dated episodes | Partial | Agent maintained | Bi-temporal edges | Temporal/entity links | Dated messages/conclusions | Configuration dependent |
| Entity resolution | Heuristics plus confirmed aliases | LLM graph resolution | Agent maintained | LLM resolution | Entity processing | Persistent peers | Extraction/resolution |
| Temporal modeling | Dates, grounded relative dates, historical beliefs | Timestamp metadata | Agent maintained | Valid/invalid plus observed time | Fact temporal ranges | Ordered scoped sessions | Graph metadata |
| Knowledge updates | Owner/slot belief succession | Explicit updates | Memory edits | Edge invalidation | Observation consolidation | Derivation/consolidation | Graph updates |
| Contradiction resolution | Partial: same owner/slot; exact history retained | Update resolver | Agent maintained | Edge invalidation | Consolidation | Dialectic and premises | Retriever/graph dependent |
| Preference memory | Separate fact kinds and standing instructions | Extracted profile memories | Memory blocks/files | Semantic facts | Profiles/opinions | Peer representations | Extracted graph facts |
| Multi-hop retrieval | Bounded entity discovery; optional two hops | Optional graph | Agent tool iteration | BFS/graph recipes | Graph spreading | Iterative dialectic | Neighborhood traversal |
| Hierarchical memory | Raw turns, units, facts, sessions, summaries | Fact/graph layers | Working/archival/file memory | Episodes/entities/communities | World/experience/observation/opinion | Messages/summaries/conclusions/cards | Chunks/entities/graphs/summaries |
| Session summaries | Extractive/optional model | Recent context | Compaction | Episode/community processing | Summarization/consolidation | Short/long summaries | Summary retrievers |
| Long-term summaries | Partial: scoped profile and protocol hierarchy | Profile memories | Curated memory files | Community summaries | Observations | Peer cards/consolidation | Graph summaries |
| Provenance | Exact source turn ids and cascading invalidation | History/metadata | Raw history and files | Supporting episodes | Source facts/links | Premise tracing | Source chunks |
| Abstention | Heuristic evidence coverage; uncalibrated | Answerer dependent | Agent dependent | Answerer dependent | Answerer/reflection dependent | Dialectic dependent | Answerer dependent |
| Reranking | Optional local cross-encoder | Configuration dependent | Agent dependent | Multiple recipes | Cross-encoder | Dialectic selection | Retriever dependent |
| 1M scaling | Bounded context; accuracy unverified | Unverified here | Unverified here | Unverified here | Unverified here | Unverified here | Unverified here |
| 10M scaling | Bounded vector scan/cache; accuracy unverified | Unverified here | Unverified here | Unverified here | Unverified here | Unverified here | Unverified here |
| Token efficiency | Caller budget; default 1,500 estimated tokens | Reported in paper | Explicit context limits | Context assembly | Recall budget | Context/representation selection | Retriever context selection |
| Storage efficiency | SQLite WAL, shared content-hash vectors, float16 | Backend dependent | Files/history | Graph backend dependent | PostgreSQL/vector indexes | PostgreSQL/vector collections | Pluggable graph/vector backends |
| Latency | Local profiles recorded separately | No comparable run | No comparable run | No comparable run | No comparable run | No comparable run | No comparable run |

## Adopted ideas and measured impact

1. **Bounded graph discovery** (Graphiti, Hindsight, Cognee): traverse existing
   indexed entity mentions from six eligible seeds, inspect at most 96 mention
   rows per hop, expand at most 16 entities per hop, skip entities with over 32
   mentions, cap candidates at 64 and hops at two. Relational questions activate
   one hop automatically, or two when nested relation clauses are detected. Reserve four discovery positions before reranking.
   Provenance is returned as `graph_paths`; candidates are exact turns, not
   invented edges or derived conclusions. No extra inference calls. A regression
   demonstrates a missing cross-session intermediate-entity result recovered
   while session filters and injection screening hold. General answer accuracy
   and latency impact remain unmeasured. `Options(graph_hops=0)` permits ablation.
2. **Historical belief selection** (Zep/Graphiti): a past-date question selects
   beliefs valid through the end of that date window instead of pinning current
   facts. Current standing instructions still govern the answer unless the
   caller explicitly requests a historical snapshot. Historical summaries are
   withheld because they may contain later evidence. Regression proves old
   residence is pinned while the current answer-format instruction remains.
3. **Provenance and revision correctness** (Zep, Hindsight, Honcho): the preceding
   implementation commit maintains separate owners, source-message support,
   chronological belief successors and atomic extraction checkpoints. Deleting
   a premise removes unsupported derived memories. History remains inspectable.
   No Honcho source was reused; these are CommonTrace schema operations.
4. **Move reusable work off the query path** (MemGPT/sleep-time rationale): keep
   shared compact vectors in the process cache keyed by persistent content
   revision, not request lifetime. Read/score units in batches when too large.
   Filtered cold searches do not embed unrelated sessions. This is deterministic
   caching, not speculative LLM reasoning. See the reproducible local profile.
5. **Avoid profile scans and high-degree expansion** (hybrid and bounded graph
   retrieval): separate fact FTS and indexed belief operations; early-stop degree
   checks after the fanout limit instead of counting an entity's full history.
   Exact metadata constraints apply before dense/sparse top-k and all derived
   evidence layers. The earlier 10,000-message local lexical profile improved
   median recall from 390.046 ms to 7.278 ms, but ingestion increased from 0.567 s
   to 0.884 s. This is a synthetic workload, not an official benchmark score.

Not adopted: wholesale competitor merges, mandatory graph database migration,
LLM speculative reasoning, unbounded graph expansion, irreversible summary-only
storage, fake calibrated confidence or benchmark-answer-specific rules. These
would require demonstrated gains and operational justification.

## Remaining limits

Dense retrieval remains an exact O(Nd) scan. Batching bounds working memory but
does not replace an ANN index at very large corpus sizes. Entity extraction is
heuristic and not a full coreference resolver. Belief revision uses explicit
owner/slot structure; free-form contradictions need stronger extraction and
semantic validation. Observed time is not a complete bitemporal event model.
Abstention coverage is not a calibrated answer probability. The 1M/10M accuracy,
category reference targets, relative competitor ranking, and cost/latency Pareto
frontier require matched official evaluation with local generation/judge models.
The benchmark directory remains required by the CLI and regression imports.
