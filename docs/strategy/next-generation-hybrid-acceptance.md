# F1 hybrid-search and restart acceptance

`benchmarks.hybrid_acceptance` times the indexed facts search used by
`commontrace.search_recipes.search`. The measured API includes FTS5 candidate
retrieval, `fact_vector_projection.search`, canonical scope and lifecycle
admission, RRF and recipe ordering. It excludes seeding, dense-index build,
cross-encoder reranking and answer generation. `--facts 1000000` is required
before the local p95 result can be compared with the `<50 ms` target. The
server target remains unsupported by this adapter: the governed fact projection
currently selects SQLite or Lance, and does not connect a Postgres full-text +
vector implementation to this API.

Use a real configured model for production evidence:

```sh
python -m benchmarks.hybrid_acceptance --facts 1000000 --trials 30 \
  --embedder bge-small --output /tmp/hybrid-1m.json
```

The run seeds a fresh synthetic fact corpus, builds the durable vector
projection, checks a dense-only canary and an FTS-only canary through the full
API, then measures warm queries. It also launches a new Python process, checks
that the content-hash vector cache returns sampled vectors without document
embedding calls, verifies the projection watermark replays no events, and
counts the query embedding separately. On a million-fact run the cache entry
count is included in the report. The output labels the embedding provider and
keeps index-build, FTS-only and vector-projection component timings outside the
hybrid p95 gate.

`--synthetic-adapter` uses fixed semantic axes so tests can prove the dense
candidate union, lexical candidate union and scope filter without downloading a
model. That run exercises the real CommonTrace APIs and storage, but its timing
is explicitly `production_api_with_synthetic_adapter`; it is not production
latency or model-quality evidence. The facts-channel `recall.recall` path is
also outside this harness because it has a separate dense-scoring path.

The call graph was traced with Graphify in CommonTrace and the available
Graphiti, Hindsight and EverOS repositories. Graphiti's
`graphiti_core/search/search_utils.py::hybrid_node_search` issues full-text and
vector searches in parallel, deduplicates each result set and applies RRF.
CommonTrace already has the corresponding bounded candidate-union and RRF
path, so no competitor code was copied. EverOS preserves a separate RRF block
for each follow-up query in `src/everos/memory/search/llm_multiround.py`; this
benchmark measures one search request and does not claim multi-round latency.

The bounded smoke command is:

```sh
python -m benchmarks.hybrid_acceptance --facts 64 --trials 2 \
  --synthetic-adapter --output /tmp/hybrid-smoke.json
```

It validates candidate and restart contracts only. A 1M local timed workload,
real-model 1M latency, Postgres/server hybrid latency and the 1M restart run
remain unmeasured until the corresponding commands are run on the intended
hardware and backend.
