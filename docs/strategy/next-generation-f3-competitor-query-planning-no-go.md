# F3 competitor query-planning follow-up

No retrieval change is justified by the competitor patterns inspected. The
existing exact `benchmarks.hybrid_acceptance` run exercises
`commontrace.search_recipes.search` at 1,000,000 facts on Lance: full hybrid
p95 was 200.82 ms, the dense projection p95 was 223.07 ms, and FTS5 p95 was
0.592 ms. The report verified dense-only and lexical-only candidates and
excluded the out-of-scope fixture. These are synthetic-adapter plumbing
measurements, not production embedding or relevance evidence. See
[`f1-hybrid-1m-synthetic-lance.json`](measurements/f1-hybrid-1m-synthetic-lance.json)
and the [acceptance contract](next-generation-hybrid-acceptance.md).

The closest competitor patterns do not safely close this gap:

- Graphiti runs full-text and vector searches through `semaphore_gather()`
  before RRF (`/root/Test/graphiti/graphiti_core/search/search_utils.py:1174-1232`).
  Adapting only that scheduling pattern can at most overlap the measured
  sub-millisecond FTS arm with the 223 ms dense arm; it cannot plausibly reduce
  p95 below 50 ms.
- Hindsight pushes its vector and BM25 arms, RRF ranks, and tenant/tag filters
  into one Postgres query (`/root/Test/hindsight/hindsight-api-slim/hindsight_api/engine/memory_engine.py:19786-19832`).
  CommonTrace's canonical facts and vectors are separate SQLite/Lance
  projections, so reproducing this would require a new governed storage
  projection and freshness/deletion contract, not a query-planning-only edit.
- EverOS sets `nprobes` on its Lance IVF search
  (`/root/Test/EverOS/src/everos/infra/persistence/backends/lancedb.py:208-229`),
  backed by IVF_FLAT indexes and a documented exactness envelope only through
  about 130K rows (`/root/Test/EverOS/src/everos/core/persistence/lancedb/base.py:43-57,206-221`).
  That setting does not transfer to CommonTrace's HNSW-SQ index. CommonTrace's
  current `refine_factor=4` is already supported by a separate matched
  million-vector recall sweep; reducing it without new recall@10 evidence would
  weaken the candidate contract (`commontrace/vector_lance.py:142`).

The query audit also found that retaining Lance handles has unresolved
cross-process freshness and fork-safety behavior; that lifecycle candidate is
recorded separately in
[`next-generation-f3-lance-handle-reuse.md`](next-generation-f3-lance-handle-reuse.md).
I did not run a new million-fact benchmark: no candidate with a credible path to
the 50 ms gate survived the source and safety review, and the root agent was
running a CPU- and memory-heavy 1M write measurement. The archived 1M report is
the exact acceptance harness baseline, not a fresh matched candidate result.

**Decision:** no code or tests changed. Keep the local hybrid gate open. A
follow-up needs a measured index/query path that removes the dense scan/open
cost while preserving canonical scope, lifecycle and evidence admission, and
must report diverse 384-dimensional recall@10 plus full-API p95 from the exact
acceptance harness.
