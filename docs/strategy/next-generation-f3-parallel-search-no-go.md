# F3 local hybrid parallel-arm diagnostic

**Decision: no retrieval change.** Graphiti's parallel full-text/vector fan-out
is safe to test as a planner experiment, but its measured gain is too small to
address CommonTrace's million-fact latency miss. The result also does not meet
the exact-recall target, so it is not acceptance evidence.

## Graphify and source review

Graphify traced CommonTrace `search_recipes.indexed_search()` through the
lexical candidate lookup, `fact_vector_projection.search()`, and
`LanceVectorIndex.search()`. Candidate IDs are still hydrated from the
canonical snapshot and checked for scope, lifecycle, authority, time, and
evidence before ranking.

The competitor graphs and source show different storage contracts:

- Graphiti's `hybrid_node_search()` runs its full-text and vector calls through
  `semaphore_gather()` and applies RRF afterward
  (`/root/Test/graphiti/graphiti_core/search/search_utils.py:1174-1231`).
- Mem0's Python `_search_vector_store()` calls dense search and then keyword
  search sequentially (`/root/Test/mem0/mem0/memory/main.py:1620-1665`); its
  Qdrant adapter exposes separate dense and BM25 calls
  (`/root/Test/mem0/mem0/vector_stores/qdrant.py:415-483`).
- Hindsight's Graphify trace reaches `.search_knowledge_pages()` and its
  `knowledge_bm25_arm()`/SQL path (`/root/Test/hindsight/hindsight-api-slim/hindsight_api/engine/memory_engine.py:19602-19832`).
  Its vector and BM25 arms, tag filters, RRF, and hydration share Postgres;
  CommonTrace's Lance index and canonical SQLite facts do not.

Only Graphiti's scheduling idea transfers without a new storage or freshness
contract. CommonTrace's archived 1M synthetic-Lance acceptance run measured
full-hybrid p95 200.82 ms, dense projection p95 223.07 ms, and FTS p95 0.592
ms. Hiding the lexical arm cannot remove the dominant dense cost.

## Matched diagnostic

I compared the current sequential planner with an in-memory Graphiti-style
overlap on one temporary 10,000-fact canonical fixture. The vector index used
LanceDB 0.40.0 and 384-dimensional deterministic hashed unit vectors. The
vectors are diverse but synthetic and do not measure semantic quality. The
query was fixed to `dense canary runtime`; reference time was fixed, reranking
was disabled, and 40 warm trials per arm alternated in ABBA order. The test
adapter was injected only into the benchmark process because the current
working-tree `embeddings.parse()` rejects newly registered custom provider
tags; no source files or retained databases were changed.

| Planner | p50 | p95 |
| --- | ---: | ---: |
| Sequential FTS then dense | 29.164 ms | 34.397 ms |
| Overlapped FTS and dense | 29.656 ms | 32.722 ms |

All 80 calls returned exactly the same ordered IDs, scores, and signal maps.
The dense-only and lexical-only canaries remained present, and the out-of-scope
canary stayed excluded. The p95 difference was 1.675 ms (4.9%) while p50 rose
0.492 ms; this small shared-host diagnostic does not establish a durable gain.

For the same 10K fixture, brute-force cosine over all persisted vectors gave
ANN recall@10 of 0.60 (6/10). The scheduling change uses the same ANN query, so
it cannot improve that recall. A separate 1M exact-vector sweep at
`refine_factor=4` reports mean recall@10 0.995 and a 0.90 minimum, but that
component sweep does not validate this synthetic hybrid corpus or close the
full-query gate. The 1M hybrid acceptance fixture itself uses repeated 4D
synthetic vectors and reports no recall metric.

Keep the parallel-arm idea as a no-go for this gate. Revisit only with a
representative 384D million-vector fixture that demonstrates exact recall@10
at the acceptance threshold and a matched full-API p95 below 50 ms. Do not
change ANN limits or canonical admission based on this diagnostic.
