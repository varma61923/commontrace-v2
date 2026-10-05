# Batched exact retrieval and local serving

Baseline: `4b555b0e3e88223b3556dab4430a2f75904f6a8f`. The implementation in
this change retains sparse/dense fusion, metadata filtering, evidence selection
and the caller's context budget. It improves repeated work in query processing.
Raw measurements and environment details are in `batched-retrieval-results.json`.

## Changes

* Encode related query facets together. Each facet retains its independent dense
  ranking before the existing hybrid RRF and per-turn maximum across facets.
* Score at most 16 queries against each 1,024-passage batch. Convert compact
  float16 corpus data once per batch, and use contiguous cached matrix views for
  unfiltered recall. Oversized corpora stream once per query batch.
* Select top-k with linear-time partitioning, then sort the winners. Explicit
  boundary ties prefer lower passage ids. A fixed-order `einsum` reduction keeps
  identical vectors consistent across row and query batch boundaries.
* Reuse query vectors across request-scoped connections through a process-local
  LRU, capped at 512 entries / 8 MiB of vector data, with five-minute reuse
  expiry. Keys contain model identity and query hashes; cached values are
  immutable vectors. Each caller receives an independent array. Identical facet
  batches coalesce across concurrent requests, including reordered batches.
* Each recall still reads one current SQLite source snapshot. Empty filtered
  scopes skip model encoding. Shared query vectors do not store source evidence;
  session filters, deletion and updated source generations continue to apply.

The query cache expires entries lazily during access. Its vector byte cap excludes
Python metadata. Model identity uses weak references and an opaque generation
token, preventing an unloaded model's object-id reuse from serving stale vectors.
Passage embeddings retain their existing separate content-hash cache and prefix
semantics. No model/provider dependency was added.

## Measurements

| Workload | Before | After | Outcome |
|---|---:|---:|---|
| 50,000 × 768-dimensional vectors, eight queries, retained matrix | 762.892 ms | 150.826 ms | 5.06× faster median; all eight top-ten lists match |
| Same retained corpus, one query | 92.676 ms | 89.430 ms | Similar single-query latency; matching top-ten list |
| 200,000 × 768-dimensional vectors, eight queries, real SQLite vector cache, matrix exceeds RAM cache cap | 22,527.588 ms | 3,158.759 ms | 7.13× faster median; matching top-ten lists |
| Offline Arctic model, 260 passages, repeated ten-facet question through new Store connections | 848.356 ms | 14.715 ms | Median of three warm requests; expected four facts retained |

The synthetic workloads use normalized deterministic vectors, seed 2026, and
one OpenBLAS thread. The 200,000-passage workload exercises the real persistent
SQLite vector cache with synthetic encoding. Both versions encode 200,000
passages during initial preparation; measured searches read the cached vectors.
The compact full matrix would occupy 307,200,000 bytes, exceeding the existing
128 MiB retained-vector cap, so neither version retains that matrix in RAM.
This tests a large vector corpus, not a 10M-token answer-quality benchmark.

The real-model check uses already cached
`Snowflake/snowflake-arctic-embed-m-v1.5` weights and local CPU inference, with
Hugging Face/Transformers offline mode and one OpenMP/OpenBLAS thread. Model
loading, corpus preparation and connection opening are outside recall timings;
HTTP transport is not measured. First uncached recall
was similar: 851.384 versus 833.513 ms. Its ten query encodes became one batch;
subsequent identical requests made zero model encoding calls. Each request used
a newly opened Store, so the per-Store response cache cannot explain this gain.
Both versions used 1,405 estimated context tokens and included venue, budget,
color and launch facts. This is a small functional/performance check, not an
answer-generation evaluation or evidence of general competitor superiority.

## Correctness and tradeoffs

A faster GEMM experiment initially measured 114 ms for eight facets, but a
regression found that GEMV/GEMM float32 reduction differences broke ties for
identical passages straddling batch boundaries. That implementation was replaced
with fixed-order scoring. The retained measurements above include that correction
and the contiguous-view optimization. Scores close to floating-point rounding
boundaries can differ from the original BLAS implementation; stable duplicate
ordering and high-precision-reference checks define the tested behavior.

Regression checks cover high-precision top-k references, exact twins at cutoffs,
cached versus streamed/filter-restricted retrieval, multiple bounded query
batches, source snapshots older than an already published index, facet answer
coverage, query-cache identity/expiry/size limits, concurrent calls, mutable
caller arrays, failed local inference, and updated/deleted/filtered evidence.

The shared cache helps repeated queries and overlapping recurring facets.
Unpredictable queries still pay local model inference cost, and exact dense
search still scans all eligible vectors. Disk streaming remains substantially
slower than retained matrices. No ANN recall tradeoff, speculative LLM call,
judge modification or answer-specific shortcut was introduced.

## Reproduction

Run the same helper from this change against an independent baseline checkout
at the SHA above and a candidate checkout:

```bash
OPENBLAS_NUM_THREADS=1 python research/profile_dense_facets.py CHECKOUT --queries 8
OPENBLAS_NUM_THREADS=1 python research/profile_dense_facets.py CHECKOUT --queries 1
OPENBLAS_NUM_THREADS=1 python research/profile_dense_facets.py CHECKOUT \
  --passages 200000 --queries 8 --persistent --runs 2
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  python research/profile_query_serving.py CHECKOUT
```

The dense helper requires NumPy. The serving helper requires the attention extra
and cached Arctic weights. Neither calls a remote inference or judging service.
Validation uses the memory regression suite, full core/end-to-end suite, Ruff,
Bandit's existing medium/high gate, the repository's existing local latency gate,
and GitHub CI on the published revision.
