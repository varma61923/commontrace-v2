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
  --seed-mode canonical_fixture --embedder bge-small --output /tmp/hybrid-1m.json
```

`canonical_fixture` uses canonical fact admission, event, metadata and FTS
projections while skipping the unrelated per-fact entity sidecar. It is a
benchmark setup mode, not a change to public ingestion. The recorded synthetic
backend commands were:

```sh
python -m benchmarks.hybrid_acceptance --facts 1000000 --trials 30 \
  --synthetic-adapter --seed-mode canonical_fixture \
  --output /tmp/f1-hybrid-1m-synthetic-sqlite.json
COMMONTRACE_VECTOR_BACKEND=lance python -m benchmarks.hybrid_acceptance \
  --facts 1000000 --trials 30 --synthetic-adapter \
  --seed-mode canonical_fixture --output /tmp/f1-hybrid-1m-synthetic-lance.json
```

The run seeds a fresh synthetic fact corpus, builds the durable vector
projection, checks a dense-only canary and an FTS-only canary through the full
API, then measures warm queries. It also launches a new Python process, checks
that the content-hash vector cache returns sampled vectors without document
embedding calls, verifies the projection watermark replays no events, and
counts the query embedding separately. The restarted process also reruns the
hybrid query and requires both the dense-only and lexical-only fixture IDs to
remain candidates while the out-of-scope ID stays excluded. A restart report
that loses either candidate or leaks the scoped-out row fails. On a
million-fact run the cache entry count is included in the report. The output
labels the embedding provider and keeps index-build, FTS-only and
vector-projection component timings outside the hybrid p95 gate.

`--synthetic-adapter` uses fixed semantic axes so tests can prove the dense
candidate union, lexical candidate union and scope filter without downloading a
model. That run exercises the real CommonTrace APIs and storage, but its timing
is explicitly `production_api_with_synthetic_adapter`; it is not production
latency or model-quality evidence. The facts-channel `recall.recall` path is
also outside this harness because it has a separate dense-scoring path. That
path now uses bounded Lance candidates for current-time recalls, scores
post-watermark writes directly, and keeps historical queries on exact
cached-vector scoring; see [the recall projection note](next-generation-recall-projection.md).

The call graph was traced with Graphify in CommonTrace and the available
Graphiti, Hindsight and EverOS repositories. Graphiti's
`graphiti_core/search/search_utils.py::hybrid_node_search` issues full-text and
vector searches in parallel, deduplicates each result set and applies RRF.
Hindsight's `hindsight_api/engine/memory_engine.py::search_knowledge_pages`
delegates BM25/vector fusion to the configured page store and hydrates the
returned IDs through Postgres; its `engine/search/fusion.py` defines the RRF
merge. This is an adjacent server-side design, not an adapter currently wired
to CommonTrace's governed fact path.
CommonTrace already has the corresponding bounded candidate-union and RRF
path, so no competitor code was copied. EverOS preserves a separate RRF block
for each follow-up query in `src/everos/memory/search/llm_multiround.py`; this
benchmark measures one search request and does not claim multi-round latency.

The bounded smoke command is:

```sh
python -m benchmarks.hybrid_acceptance --facts 64 --trials 2 \
  --synthetic-adapter --output /tmp/hybrid-smoke.json
```

It validates candidate and restart contracts only. The 1M synthetic-adapter
results are recorded below; they do not establish real-model latency or quality.
Server hybrid is unsupported by this API, and a real-model 1M run remains
unmeasured.

The 64-fact synthetic smoke (`--facts 64 --trials 2 --synthetic-adapter`)
measured 14.16 ms p95 across two samples and verified the restart candidate and
scope checks. The full 1M synthetic fixture was then run through the same
`search_recipes.search` path with both local vector backends, 30 warm trials,
one warmup, and a fresh-process restart check:

| Backend | Seed | Projection build | Search p50 | Search p95 | Local <50 ms gate |
| --- | ---: | ---: | ---: | ---: | --- |
| SQLite | 756,528 ms | 123,363 ms | 2,797.91 ms | 2,828.93 ms | **Miss** |
| Lance | 756,528 ms | 168,518 ms | 169.16 ms | 200.82 ms | **Miss** |

Both reports are `production_api_with_synthetic_adapter`, not real-model
latency: the fixed adapter is named `synthetic:fixture-v1`, and the corpus
contains duplicate vectors for neutral fixture rows. Lance emitted empty
k-means cluster warnings for those duplicates. Each backend passed the
1M-fixture fresh-process restart contract: all 1,000,000 content-hash cache
entries were present, projection replay was zero, document embedding requests
were zero, one query embedding was counted, both expected candidates survived,
and the out-of-scope ID was excluded. The restart result does not turn the
latency miss into a gate pass.

Postgres/server hybrid search remains unsupported by this API: there is no
governed Postgres full-text + vector adapter wired into `search_recipes.search`.
The Hub's `memory_records` search already demonstrates scoped sparse/dense
candidate fusion and grounding, but its records are a different authoritative
model: Hub `MemoryRecord`s are partitioned by authenticated `org_id`, while
CommonTrace fact retrieval is governed by local `AtomicFact` events and their
scope, temporal, authority and evidence checks. There is no AtomicFact sync
outbox or source watermark for the Hub projection, and the Postgres vector API
is async while the current `search_recipes` vector callback is synchronous.
The Hub path now loads dense ANN candidate rows with bounded `id IN (...)`
queries of at most 500 IDs, applying tenant, exact scope, lifecycle and kind
filters before hydration; Python visibility and derivation checks still run
before results return. A fake-session contract verifies the batching at 501
candidates, but the Hub's real-Postgres tests were skipped because a local test
database is unavailable, and no server latency was measured.
Reusing Hub records as fact candidates would therefore have no freshness
contract. A safe adapter needs an owner-pinned projection fed from canonical
fact events with replay/deletion watermarks; each bounded FTS and ANN arm must
filter by authenticated owner and exact scope before RRF, then hydrate candidate
IDs from the canonical fact snapshot and pass them through the existing
`_eligible_fact`/`EvidenceResolver` path before graph expansion or return.
The Hub integration fixture requires a real Postgres database and drops its
test schema; its database-backed tests were skipped in this local audit.

Real-model local latency, server latency and answer quality remain unmeasured.
The exact reports, source hashes and commands are archived in
[the SQLite report](measurements/f1-hybrid-1m-synthetic-sqlite.json) and
[the Lance report](measurements/f1-hybrid-1m-synthetic-lance.json); verify the
adjacent `.sha256` sidecars before using the aggregates.

The report's diagnostic component timings point to dense search as the main
local bottleneck. At 1M, dense projection p95 was 2,848.97 ms on SQLite and
223.07 ms on Lance; FTS5 candidate p95 was 0.436 ms and 0.592 ms respectively.
A separate 10K SQLite fixture measured full API p95 46.21 ms, dense p95
44.21 ms and FTS5 p95 0.321 ms. Its [raw report](measurements/hybrid-small-baseline-sqlite.json)
records all five samples, the benchmark command and source hashes. This is a
diagnostic fixture, not the 1M gate. The current SQLite vector implementation
streams the exact vector corpus and scores it in Python when no published
snapshot is available, which explains its linear read cost. The 1M Lance
fixture uses the synthetic adapter's four-dimensional repeated axes, so its
latency does not establish performance on representative 384-dimensional
vectors. A useful next measurement needs diverse vectors (or the configured
embedding model) and must report exact recall@10 alongside scope and lifecycle
checks before changing ANN limits or relying on an index cache.
