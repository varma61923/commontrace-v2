# Competitive analysis: agent-memory frameworks vs. CommonTrace

**Scope.** Twenty-three agent-memory-adjacent projects were cloned and read
at the source level — not benchmarked from their docs, not summarized from
blog posts or landing pages. Every claim below cites a specific file (and,
where useful, a function or line) in the competitor's own repository, at the
commit pinned in the tables below. Where CommonTrace already does the
equivalent thing, or has deliberately chosen not to, that is stated as
plainly as the gap itself — this is not a "why we're better" document, it's
a "what exists elsewhere that this codebase does not, and whether that's an
oversight or a choice."

This report was produced in two passes. The first (§1.1–§1.6, §2, §3.1–§3.7)
covered six memory frameworks closest to CommonTrace's own niche. The second
pass (§1.7 onward, §3.8 onward) widens the aperture to vector databases,
graph-memory systems, and newer commercial memory-as-a-service products, to
check the first pass's conclusions against a broader sample rather than
letting six repos stand in for the whole field.

### Pass 1 — closest-niche memory frameworks

| Project | Repo | Commit pinned | What it optimizes for |
|---|---|---|---|
| **mem0** | `mem0ai/mem0` | `0df3e4b` (2026-09-16) | Fast per-user chat personalization; largest community |
| **Graphiti** (Zep) | `getzep/graphiti` | `c035afb` (2026-09-11) | Bi-temporal knowledge graph; "what was true when" |
| **Letta** (MemGPT) | `letta-ai/letta`, `archive` branch | `56ba9c2` (2026-08-13) | Agent-editable memory blocks; stateful runtime |
| **LangMem** | `langchain-ai/langmem` | `9d033b4` (2026-09-08) | Prompt/behavior optimization from trajectories |
| **Cognee** | `topoteretes/cognee` | `c0d18c8` (2026-09-09) | Multi-agent shared knowledge graph, ingestion pipeline |
| **Memobase** | `memodb-io/memobase` | `358c16b` (2026-01-11) | Structured per-end-user profiles |

### Pass 2 — vector infra, graph/temporal memory, and commercial SDKs

| Project | Repo | Commit pinned | What it optimizes for |
|---|---|---|---|
| **Chroma** | `chroma-core/chroma` | `ec22d99` (2026-09-16) | Embedded/server vector store; ANN + full-text |
| **Milvus** | `milvus-io/milvus` | `f547512` (2026-09-17) | Distributed vector DB at billion-vector scale |
| **pgvector** | `pgvector/pgvector` | `efa08fd` (2026-09-08) | Vector search as a native Postgres extension |
| **Qdrant** | `qdrant/qdrant` | `6ab21ca` (2026-09-03) | Filtered ANN at scale; payload-aware HNSW |
| **Honcho** | `plastic-labs/honcho` | `36881ff` (2026-09-16) | Per-user "theory of mind" representation + dreaming |
| **LlamaIndex Memory** | `run-llama/llama_index` | `fd4a517` (2026-09-15) | Pluggable memory-block abstraction for RAG agents |
| **Memary** | `kingjulio8238/Memary` | `b2331a2` (2024-10-18) | Neo4j knowledge-graph agent memory |
| **Memoripy** | `caspianmoon/memoripy` | `42316ca` (2026-08-17) | Multi-signal (BM25+semantic+temporal+trust) fused ranking |
| **memU** | `NevaMind-AI/memU` | `08e1ed4` (2026-09-10) | Markdown-wiki memory folders for AI companions |
| **Zep** | `getzep/zep` | `495bf72` (2026-09-11) | Ingestion/eval tooling around a hosted temporal graph |
| **Supermemory** | `supermemoryai/supermemory` | `c927c98` (2026-09-16) | Always-on capture middleware + memory-graph UI |
| **Hindsight** | `vectorize-io/hindsight` | `bcf1eb3` (2026-09-17) | Human-like memory: recency decay, opinions, retraction |
| **Dakera** | `Dakera-AI/dakera-py` | `9d6a192` (2026-09-13) | REST client for a hosted vector+graph memory API |
| **EverOS** (Evermind.ai) | `EverMind-AI/EverOS` | `5076683` (2026-09-08) | Local-first, Markdown-native, self-evolving memory |
| **Maximem Synap** | `maximem-ai/maximem_synap_sdk` | `e0402c9` (2026-09-14) | Broadest agent-framework SDK integration surface |
| **MemoryLake** | `memorylake-ai/memorylake-cli` | `31f9638` (2026-09-14) | CLI ergonomics over a hosted memory SaaS |
| **XTrace** | `XTraceAI/xtrace-sdk` | `0f8522e` (2026-04-01) | Client-side encrypted memory (AES-GCM + homomorphic search) |

Three pass-2 entries (Dakera, Maximem Synap, MemoryLake) are commercial
products whose actual memory *engine* is closed-source; what's cloned and
read here is only their public client SDK/CLI, so their sections judge
integration surface and tooling, not retrieval algorithms.

CommonTrace occupies a distinct niche from nearly all twenty-three: it is
not a chat-memory layer for personalizing conversations with an end user,
and it is not a vector database. It is a **protocol for turning one agent
fleet's failures into governed, causally measured, reusable rules**
(`protocol/PROTOCOL.md`) — closer to an experimentation/observability
platform for a coding or support fleet than to a personal-assistant memory
or a general-purpose ANN index. That framing matters for reading the gaps
below: some of them (Memobase's per-user profiles, Honcho's peer cards,
LangMem's prompt rewriting, Milvus/Qdrant's distributed-scale infra) are
close to **out of scope** for what this product claims to be, or
disproportionate to its actual corpus sizes (hundreds to low-thousands of
lessons per org, not millions-to-billions of vectors or a continuous
chat-message firehose), and are marked as such rather than as omissions.

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

### 1.7 Chroma — filtered ANN, and a quantized cluster index for scale

Chroma is a "collection" store that has grown, underneath Python
convenience APIs, a real Rust ANN/quantization stack far beyond a
brute-force cosine scan:

- **True filtered ANN, not post-filter-then-truncate.**
  `chromadb/segment/impl/vector/local_hnsw.py: LocalHnswSegment.query_vectors()`
  computes `allowed_ids` from a metadata pre-filter
  (`chromadb/segment/impl/metadata/sqlite.py`, SQL `where`/`where_document`
  predicates) and passes a `filter_function` closure directly into
  `hnswlib.Index.knn_query(..., filter=filter_function)` — the ANN graph
  traversal itself skips disallowed labels, rather than over-fetching and
  discarding.
- **Clustered + quantized index (SPANN) for scale beyond single-machine
  HNSW.** `rust/index/src/spann/quantized_spann.rs: QuantizedSpannIds` /
  `QuantizedDelta` implements an IVF-like design: vectors assigned to
  `PREFIX_CENTER` cluster centroids, stored as rotated+quantized `Code`,
  with balance/split/merge/reassign logic (`MAX_BALANCE_DEPTH`) for
  incremental rebalancing as data grows.
- **Full-text index with maxscore pruning as a first-class citizen**:
  `rust/index/src/{full_text.rs,maxscore.rs}` implement BM25-like scoring
  with WAND/MaxScore-style early termination over block-file-backed,
  incrementally-updatable corpora.

### 1.8 Milvus — hybrid dense+sparse fusion as a server-understood primitive

Milvus is a distributed vector database; most of its ANN math lives in a
separate C++ library (knowhere, linked via cgo, not vendored in the Go
repo), but the Go layer reveals which capabilities it exposes as
first-class product surface:

- **Native hybrid dense+sparse fusion at query time.**
  `client/milvusclient/reranker.go` defines `rrfReranker`
  (`NewRRFReranker`, default `K: 60`) and `weightedReranker`
  (`NewWeightedReranker`), which merge multiple independent ANN result
  lists (e.g. one dense-vector search + one sparse/BM25 search) — a
  server-understood "hybrid search" concept, not an ad hoc client-side
  merge.
- **Server-side BM25 as a schema-attached function**: collection schemas
  can declare a `FunctionType_BM25` function that generates sparse vectors
  automatically, alongside a dedicated `SparseInvertedIndexAlgo` param
  family.
- **Broad ANN index catalog with GPU variants**, each validated by its own
  checker (`internal/util/indexparamcheck/{hnsw,ivf_pq,ivf_sq,cagra,
  raft_ivf_pq}_checker*.go`) — HNSW, IVF+PQ, IVF+SQ, and GPU-accelerated
  CAGRA/RAFT.
- **Growing vs. sealed segments for incremental updates without index
  rebuilds** (`internal/util/segcore/segment_interface.go`), plus
  partition/database-level multi-tenant isolation
  (`internal/metastore/model/{partition.go,database.go}`).

### 1.9 pgvector — the lowest-friction on-ramp, because CommonTrace's Hub is already Postgres

pgvector is the most directly comparable of the four vector engines, since
CommonTrace's Hub already runs Postgres for everything else:

- **Incremental graph inserts, no full rebuild.** `src/hnswinsert.c:
  HnswInsertTupleOnDisk()` acquires a page-level lock, finds neighbors via
  `HnswFindElementNeighbors()`, and updates the on-disk HNSW graph on every
  `INSERT` — no "reindex the whole corpus" step.
- **Iterative scan fixes the classic filtered-ANN under-return problem.**
  `src/hnsw.c`'s `hnsw.iterative_scan` GUC (consumed in
  `src/hnswscan.c`) re-expands `ef_search` and keeps walking the graph when
  a `WHERE` clause filters out most ANN candidates, instead of silently
  returning fewer than `k` rows.
- **Built-in compression/quantization types, no separate library needed.**
  `src/halfvec.h` (16-bit float vectors), `src/bitvec.h` (binary vectors
  for Hamming-distance ANN), and `src/sparsevec.h` (sparse vectors with
  their own HNSW opclass) all ship as native Postgres types indexable by
  the same HNSW/IVFFlat machinery.

### 1.10 Qdrant — cardinality-aware filtered HNSW, the state of the art for "filter + vector search at once"

Qdrant's segment engine is built specifically around "ANN + filter" as one
query — the single feature area none of the other three vector engines
solve as thoroughly:

- **Cardinality-aware filtered search, not filter-then-scan.**
  `lib/segment/src/index/hnsw_index/hnsw/vector_index_impl.rs:
  HNSWIndex::search()` dispatches based on filter selectivity (tracked via
  telemetry buckets `unfiltered_hnsw`, `filtered_small_cardinality`,
  `filtered_large_cardinality`, `filtered_plain`, `filtered_exact`):
  low-cardinality filters fall back to exact/plain scan, selective-but-not-
  tiny filters use a **payload-aware HNSW** — a second, smaller set of
  graph links built per payload/tag subgraph so filtered traversal doesn't
  degrade to a near-linear scan.
- **Multiple quantization schemes as first-class, swappable config**:
  `lib/segment/src/vector_storage/quantized/quantized_vectors.rs` handles
  product, binary (with 1/1.5/2-bit encodings), and scalar int8
  quantization, all layered under the same HNSW index.
- **Sparse vectors get a dedicated index type for hybrid search**
  (`lib/segment/src/index/sparse_index/`), structurally separate from the
  dense HNSW index, letting one collection combine both and fuse at query
  time.
- **Shard-key based multi-tenant isolation**
  (`lib/collection/src/collection/sharding_keys.rs`) routes points to
  shards by a tenant key, isolating one tenant's filtered-search
  cardinality from another's at the storage layer.

### 1.11 Honcho — a per-user representation store plus an offline "dreamer"

Honcho is the deepest system in the memory-framework sample: a per-(observer,
observed) "representation" store plus a background mining pipeline, sitting
behind a tool-calling dialectic chat agent.

- **A structured per-user profile tier.** `src/deriver/deriver.py:
  process_representation_tasks_batch` runs an LLM pass per message batch
  that writes into a peer-pair-scoped representation;
  `src/utils/representation.py` defines `ExplicitObservation`/
  `DeductiveObservation` (explicit vs. inferred facts, each with
  timestamps/session metadata). `src/crud/peer_card.py: get_peer_card`
  rolls this into a persistent biographical summary consulted at query
  time (`src/dialectic/workspace.py`).
- **Consolidation is an elaborate offline "dream" pipeline**, not just a
  report: `src/dreamer/surprisal.py: sample_observations_with_surprisal`
  scores observations by geometric surprisal using five different
  pluggable nearest-neighbor structures (`src/dreamer/trees/` — cover
  tree, RP-tree, LSH, prototype, sklearn wrapper) just to decide which
  observations get a second LLM look; `dream_scheduler.py`/`dream_due.py`
  gate when a peer-pair's corpus is "due."
- **Real multi-framework adapters**: full LangGraph and CrewAI examples
  (`examples/langgraph/`, `examples/crewai/python/src/honcho_crewai/`)
  wired as memory/tool backends, not just documentation.

### 1.12 LlamaIndex Memory — a pluggable abstraction, not a competing product

The interesting code is LlamaIndex's composable "memory block" system and
token-budget flush logic, not a RAG feature:

- **Structured fact extraction with LLM-driven condensation.**
  `llama_index/core/memory/memory_blocks/fact.py:
  FactExtractionMemoryBlock._aput` extracts `<fact>` tags per turn, and
  once the fact count exceeds a threshold, re-summarizes the *whole* fact
  list via an LLM prompt — an automatic, unsupervised condense/merge step
  with no approval gate.
- **Priority-based block truncation.** `llama_index/core/memory/memory.py`
  drops memory blocks below the token budget lowest-priority-first, and
  `chat_summary_memory_buffer.py: _summarize_oldest_chat_history`
  recursively LLM-summarizes the oldest turns once the token limit is
  exceeded.
- **Multi-backend plug surface.** `llama-index-integrations/memory/
  llama-index-memory-{mem0,bedrock-agentcore}` let the same `BaseMemory`
  interface swap in mem0 or AWS AgentCore as the actual store — a genuine
  adapter point other memory vendors plug into.

### 1.13 Memary — knowledge-graph memory, and a cautionary bug

A knowledge-graph-centric agent memory library, useful both as a positive
example (real graph memory) and a negative one (uncritically-trusted
ranking code):

- **Graph/entity-relationship memory via external Neo4j.**
  `src/memary/agent/base_agent.py` wires `Neo4jGraphStore`,
  `KnowledgeGraphIndex`, and `KnowledgeGraphRAGRetriever` for full
  triplet-graph memory.
- **Frequency/recency importance weighting for entities.**
  `src/memary/memory/entity_knowledge_store.py:
  _update_knowledge_memory` increments a count and bumps a date each time
  an entity recurs across sessions.
- **A real bug, worth citing as a caution.** `base_agent.py:
  _select_top_entities` does `np.argsort(entity_counts)[:TOP_ENTITIES]` —
  ascending sort sliced from the front picks the *lowest*-count entities,
  the opposite of "top" by frequency, despite being presented as
  importance-weighted ranking.

### 1.14 Memoripy — multi-signal fusion ranking, with a skeptical caveat

Far beyond the "lightweight memoripy" reputation, this is a large
multi-signal fusion retrieval engine with auto-consolidation:

- **Reliability/usage signals already feed ranking.**
  `memoripy/retrieval.py: rank_records` fuses BM25, semantic cosine,
  exact-match, and temporal scores via reciprocal-rank-fusion, then adds a
  `trust_bonus`/`utility_bonus` and subtracts a `dormancy_penalty`/
  `contradiction_penalty` — a memory's track record and staleness directly
  change its rank.
- **Auto-applied consolidation, no human gate.**
  `memoripy/engine.py: MemoryEngine.consolidate` clusters episodic
  memories and automatically promotes/merges them into semantic records
  within a time budget — no review step.
- **Temporal-expression parsing.** `memoripy/temporal.py:
  infer_temporal_bounds` resolves phrases like "2 days ago" or "since
  2024-01-01" into bounded intervals used by the temporal ranking lane.
- **Skeptical flag:** the trust/utility bonuses and penalties are
  numerically tiny (0.002–0.02) relative to the RRF term, while the
  surrounding machinery (per-lane receipts, breakdown dicts) is extensive
  — plausibly a lot of scoring theater around signals that barely move
  final rank order in practice.

### 1.15 memU — a markdown-wiki memory system, and an explicit rejection of graph memory

memU's most useful artifact is its own architecture-decision-record trail,
not its retrieval code:

- **memU's own team rejected graph/entity memory as premature complexity.**
  `docs/adr/0007-three-independent-memory-lines-wiki-graph.md` proposes
  then supersedes a graph/GraphRAG design, concluding "without multi-hop
  traversal, an entity index is not a graph — it is only a ranking
  feature, and it did not justify graph machinery here," landing on flat
  hybrid embedding+BM25 retrieval instead.
- **Broad multi-host integration surface.** `docs/adr/
  0013-self-updating-instruction-templates.md` and
  `src/memu/hosts/templates.py` describe a managed instruction block
  installed into many coding-agent hosts (Claude Code, Cursor, Codex,
  etc.), refreshed from a hosted template server.
- **Capture is scheduled, not in-task self-editing** — matching
  CommonTrace's own lifecycle-point model rather than an agent editing its
  own memory mid-task.
- **Skeptical flag:** `src/memu/hosts/claude_records.py:
  classify_claude_record` couples memory capture to one host's specific
  transcript JSON schema — brittle, and `memorize_workspace` (diff-syncing
  a whole folder, cascade-deleting stale memory on file removal) adds
  significant complexity for a capture feature.

### 1.16 Zep — bitemporal facts and a real, runnable benchmark harness

This clone is client-side ingestion/eval tooling around Zep's hosted
Graphiti graph engine, not the graph engine itself, but that tooling makes
the engine's temporal-graph and benchmark posture visible:

- **Bitemporal fact edges are a first-class, validated API concept.**
  `ingestion/src/zep_ingest/triples.py: FactTriple` has explicit
  `valid_at`/`invalid_at`/`created_at` fields, each timestamp-validated,
  mapping onto `graph.add_fact_triple`.
- **Continuous, per-turn conversation ingestion is a supported, real-time
  path.** `ingestion/src/zep_ingest/threads.py` (`ThreadMessage`,
  `thread.add_messages`) pushes chat turns into a user graph as they
  happen, auto-splitting long turns — qualitatively different from a
  one-time batch importer.
- **Benchmark methodology is real, runnable code, not slideware.**
  `benchmarks/locomo/benchmark.py` and
  `benchmarks/longmemeval/zep_longmem_eval.py` wire an actual
  ingestion/evaluation runner against the public LOCOMO and LongMemEval
  datasets, with populated experiment-output directories proving these
  were actually executed.
- **Skeptical flag:** the fact-triple path requires a pre-defined ontology
  and entity typing before ingest — justified at Zep's scale, pure
  overhead for a flat, tag-based lesson model.

### 1.17 Supermemory — always-on capture middleware, no shipped benchmark

This clone is dashboard/SDK/MCP code around a closed-source engine, but the
SDK reveals a genuine live-ingestion pattern:

- **Real live-conversation ingestion pipeline.**
  `packages/tools/src/vercel/middleware.ts` wraps every AI-SDK
  `LanguageModel` call: it injects retrieved memories pre-call and
  automatically posts the full turn (including tool calls) to
  `/v4/conversations` after every response when `addMemory: "always"`
  (the default) — genuinely watching a live conversation stream, not
  requiring an explicit "propose a memory" call.
- **Memory lineage is a linear version chain, not a bitemporal graph.**
  `packages/memory-graph/src/canvas/version-chain.ts` walks
  `parentMemoryId` links and flags forgotten/latest state — a real UI
  improvement (graph visualization) over a flat edit history, but not an
  interval-based fact model; no `validFrom`/`validTo` fields exist
  anywhere in its type definitions.
- **"Memorybench" is documentation, not shipped harness code.** Its
  benchmark and "MemScore" metric are described only in `.mdx` docs; no
  runnable eval script exists anywhere in the repo (confirmed by
  repo-wide search) — unlike Zep's actual LOCOMO/LongMemEval runners.
- **No client-side/E2E encryption here either** — TLS in transit plus
  server-side encryption at rest, the same posture as CommonTrace's Hub.

### 1.18 Hindsight — recency decay as a real algorithm, and a lot of adjacent machinery

Unlike Zep/Supermemory, the actual memory engine ships in this repo, and
it's a heavyweight temporal/causal/opinion system built for a continuous
firehose of observations:

- **Recency decay is real, algorithmic, and wired into ranking.**
  `hindsight_api/engine/search/reranking.py: compute_recency_decay` maps
  memory age to a freshness signal via a configurable linear/exponential
  curve and folds it multiplicatively into the cross-encoder rerank score
  — a more principled time-based decay than a static importance field or
  usage counter.
- **Temporal-aware fact lifecycle, short of a full bitemporal graph.**
  `hindsight_api/engine/consolidation/consolidator.py: _TemporalBounds`
  merges occurrence spans when deduplicating observations, and
  `engine/reflect/retractions.py` cascades fact invalidation into
  synthesized documents that cited the now-dead fact.
- **A genuinely rigorous, runnable eval methodology.**
  `hindsight-system-evals/evals/test_01_knowledge_page_convergence.py`
  drives the real API plus an LLM judge across waves of accumulating data
  specifically to catch regressions where an update silently un-writes a
  fact (a real caught bug is documented in its own README).
- **Skeptical flag:** a causal-link taxonomy, "disposition traits" that
  drive LLM-formed opinions, and a standing entity-resolution/graph-
  maintenance subsystem are all machinery built for a high-volume,
  continuously-observing companion agent — a lot of surface area for a
  corpus small enough that BM25 already works well.

### 1.19 Dakera — a wide, thin REST client, one non-framework integration

`src/dakera/client.py` (3,689 lines) exposes 60+ methods, but every one is
a thin pass-through to a hosted API — all retrieval/consolidation/graph
logic is server-side and closed. `src/dakera/integrations/` contains
exactly one file, and it is not an agent-framework adapter: it wires
Dakera as a storage backend for a third-party governance/cost-tracking
middleware. No LangChain/CrewAI/etc. adapter code exists anywhere in the
repo, and no Docker/K8s/Helm tooling ships in this repo either (referenced
only as a separate, unexamined `dakera-deploy` repo).

### 1.20 EverOS — the one genuinely open, local-first competitor

Markdown-on-disk is the literal source of truth here, with real background
self-organization logic and a runnable multi-benchmark harness:

- **Storage is plain Markdown with Pydantic-validated YAML frontmatter**
  (`core/persistence/markdown/frontmatter.py`); SQLite and a LanceDB
  vector index are explicitly documented as *derived, rebuildable* caches,
  not sources of truth — architecturally the same claim CommonTrace's
  local store makes.
- **Real self-evolution logic, not just files.** A documented "Reflection"
  pipeline (Select → Merge → Re-extract → Deprecate) runs as a scheduled
  offline job that clusters semantically-similar episodes, LLM-merges them
  into one consolidated narrative, and soft-archives originals via a
  `deprecated_by` frontmatter pointer.
- **A runnable benchmark harness beats a single custom methodology.**
  `benchmarks/run.py` is a real typed ADD→SEARCH→ANSWER→JUDGE pipeline
  with adapters for four external, public benchmarks (LOCOMO,
  LongMemEval, EverMemBench, SubtleMemory).

### 1.21 Maximem Synap — the broadest multi-framework integration surface examined

By far the widest agent-framework adapter surface of any project in this
report, though most of that breadth is a repeated thin-wrapper pattern:

- **25 separate integration packages** — LangChain, LangGraph, CrewAI,
  AutoGen, Google ADK, Semantic Kernel, OpenAI Agents, Microsoft Agent
  Framework, Pydantic AI, Smolagents, Strands Agents, Camel-AI, Agno,
  Mastra, Vercel AI SDK, LlamaIndex, Haystack, LiveKit Agents, Pipecat,
  NeMo Agent Toolkit, DeepAgents, and the Claude Agent SDK (Python + TS).
- **Depth is uneven.** LangChain's package has genuine framework-specific
  classes; most others ship only a `short_term.py` + `tools.py` pair — the
  same `search_memory`/`store_memory` tool wrapper reshaped per SDK's
  tool-calling convention, built on a shared common package.
- **Dual gRPC/HTTP transport with a real resilience layer**
  (`packages/sdks/maximem-synap/maximem_synap/{transport,resilience}/`) —
  genuine client-side engineering, not just a REST wrapper.
- Also ships its own MCP server, competing on both native-SDK and MCP
  fronts simultaneously.

### 1.22 MemoryLake — polished CLI ergonomics, zero deploy tooling, no local mode

A well-engineered Rust CLI that is purely a JSON-in/JSON-out client for a
hosted SaaS:

- Every command is a thin REST call struct that prints the API response as
  pretty JSON (`crates/core/src/api/**/*.rs`); the CLI ergonomics
  themselves are genuinely good — aliased subcommands, SSE streaming
  support, and an interactive TTY auth flow with SHA-256-verified,
  CI-friendly non-interactive install.
- **No Docker, Helm, or k8s manifests exist anywhere in the repo** — no
  self-host story at all, despite "self-hostable" competitive positioning
  concerns; every command requires the hosted API and workspace
  credentials, with no local storage or offline mode.

### 1.23 XTrace — the one repo with a real end-to-end encryption architecture

The one competitor whose "encryption" claim is architecturally
substantive, not marketing:

- **Content encryption is genuinely client-side.**
  `x_vec/crypto/encryption/aes.py: AESClient` does AES-256-GCM with a
  per-operation random nonce; the key comes from a scrypt-derived
  passphrase or AWS-KMS envelope encryption, and in both paths the
  plaintext key material never leaves the client / is never persisted
  server-side.
- **Search happens without server-side decryption.**
  `x_vec/retrievers/retriever.py: nn_search_for_ids()` binarizes and
  homomorphically encrypts the query embedding client-side, sends only
  ciphertext to the server's Hamming-distance computation, and decodes the
  encrypted distances client-side — the server computes nearest-neighbor
  distance over data it cannot read, backed by real (not stub) Paillier /
  Goldwasser-Micali homomorphic schemes.
- **Embedding is also computed client-side before any network call** — a
  local embedding model produces the binary vector, which is encrypted
  alongside the raw content before upload; plaintext text or vectors never
  leave the client.

---

## 2. Where CommonTrace already stands (so the gaps below are read in context)

This is not a green-field comparison — three of the mechanisms in §1.1–§1.6
are things this codebase built *from* researching those competitors, in an
earlier session, and are already merged to `main`:

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

What none of the twenty-three competitors have, and CommonTrace's whole
value proposition rests on: a **randomized holdout** that measures whether
a memory *causes* a better outcome, not just whether it correlates with one
(`commontrace/holdout_io.py`, `commontrace experiment`, `PILOT.md`). Every
competitor examined either has no causal measurement at all, or (Cognee's
`eval_framework`, Zep's/EverOS's LOCOMO/LongMemEval runners, Hindsight's
convergence evals) measures *retrieval/answer quality* against a labelled
benchmark, which is a different question from "did injecting this specific
memory change this specific fleet's specific outcome." That is the one
capability none of them ship, and it is the one this codebase is built
around.

Two things worth stating plainly after the wider pass-2 sample:

- **memU's own team, independently, reached the same conclusion
  CommonTrace's design already reflects**: their ADR
  (`docs/adr/0007-...md`, §1.15) explicitly rejects building a property
  graph over their memory units, for essentially the same reason
  CommonTrace's `STRATEGY.md`/`consolidate.py` design does — a flat,
  contradiction-and-redundancy-aware corpus answers the questions that
  matter without a graph database as a new operational dependency. This is
  independent external corroboration, not just this codebase's own
  reasoning.
- **CommonTrace's local store (plain Markdown + YAML frontmatter,
  `commontrace/lesson_io.py`) is already at parity with EverOS's core
  storage claim** ("files on disk, not a hosted database") — the honest
  gap against EverOS specifically is not storage format, it's the
  *scheduled consolidation pipeline and public-benchmark harness* built on
  top of that storage (§3.9, §3.11 below), not the storage choice itself.

---

## 3. Gaps: concrete, code-referenced, not yet addressed

Ordered by estimated value ÷ effort, highest first, within each pass.

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
automatically and continuously. Memoripy's `rank_records` (§1.14) fuses a
`trust_bonus`/`dormancy_penalty` into a multi-lane RRF score, and
Hindsight's `compute_recency_decay` (§1.18) shows the same principle
applied to *age* rather than *reliability* — three independent competitors
converge on "a memory's track record or freshness should move its rank,"
not just gate it (as CommonTrace's dosage/redundancy gates do) or report it
separately (as `commontrace reliability` does today).

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
eligibility. Memoripy's caveat (§1.14: its own bonus terms are numerically
tiny enough to plausibly never move rank order in practice) is a concrete
warning to size this adjustment large enough to matter and verify that
empirically, not just add the wiring and assume it works.

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
top of retrieval, with a runner and dashboard. Hindsight's
`test_01_knowledge_page_convergence.py` (§1.18) is a sharper version of the
same idea, purpose-built to catch silent regressions across waves of
accumulating data rather than a static one-shot score.

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
`created_at`/`expired_at` on the fact itself (§1.2), and Zep's
`FactTriple` (§1.16) validates those same four fields at the API layer as
a first-class, required concept — a real interval, over stored text,
queryable at any past instant. Hindsight's `_TemporalBounds` (§1.18) shows
a lighter-weight version of the same idea (merged occurrence spans,
without a full bitemporal graph) that costs less to build than Graphiti's
full model.

**This is explicitly named as future work by the codebase itself**, not a
silent gap — the fix (store the actual prior text, not just its hash, in
the revision journal) is a natural, bounded extension of `lesson_io.py`'s
existing journal, and would upgrade `environments.py`'s "which release is
current" answer into "what did this environment actually serve on any
past date." Given the pass-2 sample, the cheaper, better-scoped version of
this to build first is closer to Hindsight's bounded occurrence-span
merge than Graphiti's full ontology-typed graph (§1.16's own skeptical
flag: Zep's ontology requirement is real overhead CommonTrace's flat
tag model doesn't need).

### 3.4 MEDIUM — No lesson-refinement suggestion for underperforming lessons

**The gap.** `commontrace reliability` labels a lesson MISCALIBRATED
("fires often, rarely helps → tighten `applies_when`") but stops at the
label. A human must open the file and rewrite it by hand; nothing proposes
*how*.

**Reference.** LangMem's prompt optimizers (§1.4,
`src/langmem/prompts/{gradient,metaprompt}.py`) take exactly this input
shape — text plus a trajectory of outcomes — and produce a rewritten
version. LlamaIndex's `FactExtractionMemoryBlock` (§1.12) and Letta's
`rethink_memory` (§1.3) both do the same kind of unattended LLM rewrite of
memory content on their own trigger, with no human review step — two more
data points for the same pattern, and two more reasons not to copy it
unattended (see §4).

**Why not just adopt these wholesale:** every one of these three
competitors optimizes an undifferentiated blob with no per-instruction
attribution and no human gate, which is the opposite of this codebase's
approval-gated, one-lesson-one-causal-effect model. The useful piece to
borrow is narrower: an *optional* `commontrace lesson suggest-revision
<slug>` that feeds a MISCALIBRATED lesson's current
`applies_when`/`do_not_apply_when` plus the evidence (`Evidence` records
`reliability.py` already collects — which occasions it fired on and how
those went) to an LLM, and writes the suggestion as a **draft the existing
approval gate still reviews** — not an auto-applied edit. This preserves
every governance property `commontrace/commands/lesson_cmd.py`'s
`run_approve` already enforces (scaffolding check, content-safety scan,
now the redundancy check) while closing "the report told me what's wrong
but not what to do about it."

### 3.5 LOW — No reranker abstraction / pluggable second-stage scorer

**The gap.** `commontrace/retrieval.py` has exactly one lexical scorer
(`idf-v2`, plus the legacy `count-v1` kept for backward compatibility) and
one fusion mode (RRF, `commontrace/retrieval_io.py`). There is no seam for
a deployment to plug in a cross-encoder or an LLM-based reranker as a
second stage over the same candidate set.

**Reference.** mem0's `mem0/reranker/` (§1.1): one `BaseReranker`
interface, four concrete implementations, selected by config. Hindsight's
cross-encoder rerank stage (§1.18) is a second, independent example of the
same seam, with recency decay folded into that same second stage rather
than the first-pass retriever.

**Why LOW rather than MEDIUM:** the core install's whole design point is
"works with PyYAML alone, degrades gracefully without the optional
`attention` extra" (`README.md`, `pyproject.toml`'s extras). A reranker
abstraction is legitimate future work for a store that already has the
`attention` extra installed and wants a second-stage boost, but it is
additive to an already-working pipeline, not a hole in it the way §3.1 is.

### 3.6 LOW / SCOPE QUESTION — No per-end-user structured profile tier

**The gap, stated as a question rather than a finding.** Memobase's
`(topic, sub_topic)` profile model (§1.6) and Honcho's peer-card/
representation split (§1.11) both answer "what do we know about *this
specific counterparty*" — a support agent's customer, a sales agent's
prospect, a chat partner. CommonTrace's Trace/Lesson model answers "what
has this *fleet* learned about doing the job in general"
(`protocol/PROTOCOL.md`) — there is no first-class notion of a durable,
structured record scoped to one external individual, updated
incrementally as new facts about them arrive.

**Whether this is a gap or a scope boundary:** `STRATEGY.md` is explicit
throughout that this product's moat is fleet-level generalization, not
per-user personalization — the two are genuinely different problems (a
customer profile does not generalize across customers; a lesson is built
to). Honcho's own investment in this tier (five different NN-index
implementations just to sample which observations deserve a second LLM
look, §1.11) is a useful illustration of how much machinery a serious
per-user-profile system actually requires once you commit to it — a good
reason to keep treating this as a scope boundary rather than a half-built
gap. Recorded here so it's an explicit decision on the record, not
recommended as work to pick up.

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

### 3.8 MEDIUM — No retrieval-time recency decay; lessons only have a static importance field

**The gap.** A lesson's frontmatter has a static `importance` value and a
monotonically-increasing `uses` counter (`commontrace/frontmatter.py`) —
nothing in `retrieval.py` treats *how long ago* a lesson was last
validated or last fired as a ranking signal the way age itself matters.
`commontrace/decay.py` does implement a real staleness concept, but it is
scoped to a different surface entirely: per its own module docstring, it
ages out stale HELPS/HURTS *value-ledger* verdicts (the number a customer
is invoiced against, `commontrace/value.py`) on a 180-day evidence
horizon, deliberately asymmetric (a stale HURTS keeps counting against the
vendor; a stale HELPS does not) so staleness can't be used to quietly
launder away a harmful lesson's billing impact. None of that logic is
wired into `retrieval.py`'s ranking path — the retrieval-time recency gap
described here is real and independent of `decay.py`, not addressed by it.

**Reference.** Hindsight's `compute_recency_decay` (§1.18) is a clean,
isolated, configurable (linear/exponential/off) function that multiplies
into a rerank score based purely on memory age. Memary's entity store
(§1.13) does a cruder version (recency + frequency count per entity),
undermined in that repo by an actual ranking bug — a caution to
unit-test any ported decay function's output ordering directly, not just
its formula.

**Suggested shape:** a narrow, optional `recency_weight` in the same
`RetrievalConfig` surface as §3.1's `reliability_weight` — small,
independently testable, off by default, and explicitly unit-tested for
correct *ordering* (oldest-ranks-lower), given Memary's cautionary bug is
exactly the kind of off-by-one/sort-direction mistake that's easy to ship
silently in this kind of code.

### 3.9 MEDIUM — No continuous/live capture pipeline; only explicit lifecycle points and one-time batch import

**The gap.** CommonTrace's capture surface is either an explicit MCP call
at a defined lifecycle point (`record_occasion`, `propose_lesson` in
`commontrace/mcp_server.py`) or a one-time batch importer for exported
logs (`commontrace/import_data.py`, JSONL/CSV/LangSmith/Langfuse/
Braintrust/OTel). There is no mode that watches an ongoing task/
conversation stream and auto-captures candidate occasions as they happen.

**Reference.** Zep's `thread.add_messages` (§1.16) and Supermemory's
Vercel AI-SDK middleware (§1.17, `saveMemoryAfterResponse`) both wrap
every model call transparently, capturing the full turn without an
explicit "please remember this" action from the caller.

**Why this is a real gap and not just a different design point:** unlike
§3.6 (per-user profiles, a genuinely different product shape), this one is
squarely in CommonTrace's own lane — the double-review coding pipeline
(`SKILL.md`) already runs Implementer A/Reviewer B through a fixed
sequence of tool calls; an optional hook that auto-drafts an occasion
record from that sequence (still gated by the existing approval flow
before becoming an active lesson) would lower the friction that currently
requires an agent to remember to call `record_occasion` at all. Scope as
opt-in instrumentation, not a background stream-watcher with its own write
path — the auto-write-without-review pattern in Supermemory's middleware
is explicitly the part not to copy (see §4).

### 3.10 MEDIUM — No end-to-end/client-side encryption option for Hub-stored content

**The gap.** `hub/` isolates orgs via Postgres row-level security and
transports data over HTTPS/MCP — the server (and its operator) can read
lesson content in plaintext. There is no option for a security-sensitive
org to hold the only key and have the Hub store ciphertext it cannot read.

**Reference.** XTrace (§1.23) is a working, non-trivial example: client-
derived keys (scrypt passphrase or KMS envelope encryption), AES-256-GCM
for content, and homomorphic Hamming-distance search so the server never
sees a decryptable vector either.

**Scoped honestly:** XTrace's homomorphic-search layer is a large, custom
cryptography surface (a from-scratch Paillier/Goldwasser-Micali
implementation) that is disproportionate engineering and audit burden for
CommonTrace to take on just to search encrypted content server-side. The
narrower, defensible version of this gap is client-side AES-GCM encryption
of a lesson's body *before* it's synced to the Hub, with the tradeoff
stated plainly: the Hub's Postgres full-text search (`hub/search.py`)
would no longer be able to rank ciphertext, so this would need to ship as
an opt-in mode for orgs that value confidentiality over server-side
search, not a default. Worth a design doc before any code, not a quick
add — flagged here as a real, unaddressed option rather than a
recommendation to build it immediately.

### 3.11 LOW — No runnable benchmark harness against public datasets (LOCOMO / LongMemEval)

**The gap.** CommonTrace's own benchmark (`commontrace/reference/
measure_performance.py`, README's "Benchmark" section) is a custom,
self-defined methodology against this project's own fixed corpus. There is
no adapter that runs CommonTrace's retrieval against a public,
third-party benchmark dataset the way a reader could independently verify.

**Reference.** Zep ships real runners for both LOCOMO and LongMemEval
(§1.16); EverOS ships four dataset adapters (LOCOMO, LongMemEval,
EverMemBench, SubtleMemory) behind one typed pipeline (§1.20).

**Why LOW rather than MEDIUM:** these public benchmarks are built for
chat-memory recall (a different retrieval shape — "recall this fact from
turn 40 of a long conversation" — than CommonTrace's "does the right
lesson get retrieved and injected for this coding task"), so a port
wouldn't be a drop-in replacement for CommonTrace's own benchmark, only a
useful, independently-checkable second data point. Worth doing as
additive credibility, not as a substitute for the causal holdout
measurement this codebase already treats as its real differentiator (§2).

### 3.12 LOW / SCOPE QUESTION — No first-class multi-agent-framework SDK adapters (LangChain, CrewAI, AutoGen, etc.)

**The gap.** `commontrace/mcp_server.py` gives CommonTrace one universal
integration surface — any MCP-speaking client (Claude Code, Cursor, Devin,
Windsurf, OpenHands) — rather than a library of per-framework native SDK
adapters. (`commontrace/adapters.py` is a different thing entirely and
should not be read as this product's answer to framework integration: it
flattens *trace-export* formats — LangSmith/Langfuse/Braintrust/OTel rows —
for the one-time `commontrace import` batch path, not a live SDK surface
an agent framework calls into at runtime.)

**Reference.** Maximem Synap ships 25 separate framework integration
packages (§1.21); Honcho ships real LangGraph and CrewAI examples
(§1.11).

**Why this is closer to a scope question than a gap:** Maximem Synap's own
breadth is mostly a repeated thin `search_memory`/`store_memory` tool
wrapper reshaped per SDK (§1.21's own skeptical flag) — exactly the shape
MCP already standardizes, for any framework that speaks it. The concrete,
narrower question worth answering is whether any of CommonTrace's actual
target fleets run on a framework that does *not* yet support MCP as a
tool-calling transport; if so, a single thin adapter for that framework
specifically (not a library of 25) would close the real gap without
recreating Maximem Synap's per-framework maintenance burden.

---

## 4. What NOT to build (stated as loudly as the gaps)

- **LangMem-style whole-prompt rewriting** (§1.4). Abandoning discrete,
  attributable lessons for a single optimized blob would throw away the
  one property (per-lesson causal measurement) that differentiates this
  product from every competitor examined here, including LangMem itself.
- **Letta-style unattended agent self-editing of active memory** (§1.3),
  and its cousins: LlamaIndex's unsupervised fact-list condensation
  (§1.12) and Memoripy's fully-automatic consolidation with no review step
  (§1.14). This codebase already tightened exactly this failure mode this
  session — an agent could previously draft *and* approve its own lesson
  with no independent check (`commontrace/approval.py`'s two-person mode
  exists because of it). Three separate competitors in this report ship
  an LLM rewriting or merging memory content on its own trigger with no
  human gate; adopting any of them as-is would be a regression, not a
  feature.
- **A full property graph over lessons** (Graphiti/Cognee/Memary's core
  data model, §1.2/§1.5/§1.13). Those projects model *entities and their
  evolving relationships* — a customer, a product, a fact that supersedes
  another. CommonTrace models *rules for how to act* — the
  contradiction/redundancy signals this codebase already has
  (`reliability.py`, `redundancy.py`) answer the questions that matter for
  that model without needing a graph database as a new operational
  dependency. **memU's own team reached this same conclusion
  independently** (§1.15's ADR) — external corroboration, not just this
  codebase's own preference.
- **A distributed vector database, sharding, or GPU-accelerated ANN
  indexes** (Milvus's coordinator/datanode/querynode cluster and CAGRA/RAFT
  GPU indexes, §1.8; Qdrant's payload-aware dual-link-set HNSW, shard
  routing, and multiple quantization schemes, §1.10; Chroma's SPANN
  clustered-quantization index, §1.7). All of this is engineering for
  millions-to-billions of vectors per tenant. CommonTrace's actual corpora
  are hundreds to low-thousands of lessons per org — a plain, unquantized
  flat index (or pgvector's `ivfflat`/exact scan without even needing
  HNSW's iterative-scan tuning, §1.9) is adequate if semantic search is
  ever added at all, and none of this distributed/quantized machinery
  should be copied.
- **Zep's mandatory ontology/entity-typing step before ingest** (§1.16),
  and **Hindsight's causal-link taxonomy, opinion-formation traits, and
  standing entity-resolution subsystem** (§1.18). Both are real,
  well-built systems — for a continuously-observing, high-volume
  companion-agent use case. Requiring an ontology or running LLM-formed
  "opinions" over a curated, human-approved corpus of hundreds of lessons
  is pure overhead with no corresponding benefit at CommonTrace's scale.
- **Honcho's five-implementation nearest-neighbor "surprisal" sampling
  zoo** (§1.11: cover tree, RP-tree, LSH, prototype, and a sklearn
  wrapper, all to decide which observations get a second LLM look). A
  legitimate design for Honcho's continuous per-user observation stream;
  disproportionate machinery for CommonTrace's `consolidate.py`, which
  already does the equivalent job as a bounded, propose-only batch report
  over a corpus small enough not to need an ANN-index selection step at
  all.
- **XTrace's from-scratch homomorphic encryption stack** (§1.23) as a
  wholesale import. Real and working, but a large custom cryptography
  surface with its own audit burden; if client-side confidentiality is
  ever pursued (§3.10), plain AES-GCM-before-upload (accepting the loss of
  server-side full-text search on encrypted content) is the proportionate
  version, not homomorphic search.
- **Copying a competitor's scoring/ranking code without independently
  re-deriving and testing it.** Memary's `_select_top_entities` (§1.13)
  reads as importance-ranking but its `np.argsort(...)[:TOP_ENTITIES]`
  actually selects the *lowest*-count entities — a real, shipped bug in a
  widely-referenced repo. Memoripy's trust/utility bonuses (§1.14) are
  numerically small enough to plausibly never move final rank order despite
  substantial surrounding machinery. Both are concrete cautions for §3.1
  and §3.8's proposed reliability/recency weighting: verify the actual
  output ordering empirically before shipping, not just the formula.

---

## 5. Summary tables

### 5a. Pass-1 memory frameworks

| Capability | mem0 | Graphiti | Letta | LangMem | Cognee | Memobase | CommonTrace |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Write-time near-dup / contradiction handling | ✅ LLM | — | — | — | — | ✅ LLM | ✅ lexical |
| Diversity-aware retrieval admission | — | ✅ MMR | — | — | — | — | ✅ hard gate |
| Corpus consolidation report | — | — | — | — | ✅ `memify` | — | ✅ `consolidate` |
| Causal (randomized) outcome measurement | ❌ | ❌ | ❌ | ❌ | ❌ (eval only) | ❌ | ✅ |
| Outcome/reliability feeds back into ranking | — | — | — | — | ✅ | — | ❌ **(§3.1)** |
| LLM-judged answer-quality benchmark | — | — | — | — | ✅ | — | ❌ **(§3.2)** |
| Point-in-time ("what did we believe when") query | — | ✅ | — | — | partial | — | ❌ **(§3.3)** |
| Automated rewrite suggestion for a weak rule | — | — | ✅ unattended | ✅ unattended | — | — | ❌ **(§3.4, gated version proposed)** |
| Pluggable second-stage reranker | ✅ | ✅ (recipes) | — | — | — | — | ❌ **(§3.5)** |
| Per-end-user structured profile | — | — | — | — | — | ✅ | ❌ **(§3.6, scope question)** |
| Session-scoped retrieval filter | ✅ | — | — | — | — | — | ❌ **(§3.7)** |

### 5b. Pass-2 vector infra, graph/temporal memory, and commercial SDKs

| Capability | Chroma/Milvus/pgvector/Qdrant | Honcho | LlamaIndex Mem | Memary | Memoripy | memU | Zep | Supermemory | Hindsight | Dakera/Maximem/MemLake | EverOS | XTrace | CommonTrace |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Real ANN / filtered-vector index | ✅ all 4 | — | — | — | — | — | — | — | — | — | LanceDB | — | ❌ (brute-force cosine only, opt-in, unwired) |
| Recency-decay ranking signal | — | — | — | partial (buggy) | ✅ | — | — | — | ✅ | — | — | — | ❌ **(§3.8)** |
| Continuous/live capture pipeline | — | ✅ deriver | — | — | — | scheduled | ✅ threads | ✅ middleware | — | — | — | — | ❌ **(§3.9)** |
| Bitemporal / fact-validity interval | — | — | — | — | partial | — | ✅ | — | partial | — | — | — | ❌ **(§3.3/§3.10 note)** |
| End-to-end / client-side encryption | — | — | — | — | — | — | — | — | — | — | — | ✅ | ❌ **(§3.10)** |
| Runnable public-benchmark harness | — | — | — | — | — | — | ✅ | ❌ (docs only) | ✅ | — | ✅ 4 datasets | — | ❌ **(§3.11)** |
| Graph/entity memory | — | — | — | ✅ Neo4j | — | ❌ (rejected, ADR) | — | — | partial | — | — | — | ❌ (deliberate, §4) |
| Broad multi-framework SDK adapters | — | ✅ 2 | — | — | — | — | — | — | — | ✅ Maximem 25 pkgs | — | — | ❌ **(§3.12, scope question)** |
| Self-hostable / local-first, no vendor lock-in | — | — | — | — | — | — | — | — | ✅ | ❌ SaaS-only | ✅ | — | ✅ (pre-existing) |

---

## 6. How this was produced

All twenty-three repositories were cloned with `git clone --depth 1` and
read directly — module trees, then targeted `grep`/`read` of the files
cited above — not summarized from README claims. Every code citation in
this document was re-verified against the pinned commit at the time of
writing; line numbers are approximate to the nearest declaration where the
competitor's own file has since reformatted. CommonTrace-side claims (the
retrieval sort key, the `environments.py` quote, the `reliability.py`
import graph, the Hub's Postgres-tsvector-only search implementation) were
checked against this repository's own source in the same session, not
asserted from memory. The pass-2 research (§1.7–§1.23, §3.8–§3.12) was
produced by four parallel research passes, each scoped to a disjoint
subset of repositories with explicit instructions to cite exact files and
functions and to flag thin wrappers, premature complexity, and any bugs
found in a competitor's own code rather than repeating its marketing
claims; this document synthesizes and cross-checks their findings rather
than reproducing them verbatim.
