# P0 local vector audit

P0 remains open: the million-fact batch gate misses 2,000 facts/s, and no
million-fact hybrid or restart measurement has been accepted. Keep those gates
separate from selective sparse timing and retrieval-quality fixtures.

Graphify traces `fact_vector_projection._index` to `SQLiteVectorIndex` by default
and to the optional `LanceVectorIndex` when configured. SQLite's dense search
streams every eligible vector through exact cosine scoring. Lance has background
HNSW maintenance, but its real optional-engine tests were skipped in the current
core environment. Validate that existing implementation before changing defaults.

Use a separate virtual environment so installing an optional engine cannot alter
the dependencies of the full application suite already running. First run the
existing real-engine owner, generation, allowed-id, deletion and new-fragment
contracts. Preserve canonical scope, temporal and revision admission. A passing
small-store test establishes behavior within that fixture only.

Then measure the real API at increasing scale with reproducible vectors and
queries, recording exact-neighbor recall as well as latency. Record index build
and update costs separately from reads. Restart a fresh process against durable
content-hash caches and count document embeddings separately from query embeddings.
Direct vector timings do not establish the required full hybrid latency; that
measurement must include retrieval fusion and canonical admission. Use named
local models for semantic-quality evidence.

Extend the existing `vector_scale_bench` harness with an optional Lance engine,
retaining its PostgreSQL default and shared seeded workload. Tests precede this
extension and cover paired query samples, explicit vector-only scope, invalid
inputs and cleanup of benchmark-owned temporary stores.

Lance's [vector-index documentation](https://docs.lancedb.com/indexing/vector-index)
describes matching query/build distance metrics, HNSW search controls and exact
search for recall comparisons. Verify the installed declared dependency range
against these contracts; documentation alone does not establish runtime support.

The two existing real-engine tests pass on LanceDB 0.40.0. The scale extension's
two tests failed before implementation; all four optional-engine/scale tests
then passed. Ruff and the benchmark's configured Bandit gate pass. The isolated
environment's dependency audit is clean after upgrading its bundled pip.

At 10,000 synthetic 384-dimensional vectors, 20 paired queries measured exact
p95 12.81 ms and ANN p95 7.15 ms, with mean and minimum recall@10 both 1.0.
Loading took 2.12 seconds and background index construction 0.93 seconds.
This is an index component experiment, excluding embedding inference, lexical
and graph fusion, temporal filters and canonical admission. It does not satisfy
the million-fact hybrid gate. The default remains unchanged. Reproduce with:

```sh
python -m benchmarks.vector_scale_bench --engine lance --n 10000 --dim 384 \
  --queries 20 --top-k 10 --clusters 200 --seed 61923 --batch 1000 \
  --out /path/to/lance-10k.json
```

The raw report binds the benchmark and product source hashes; the benchmark
extension was uncommitted at launch. Keep source and scale distinctions visible
when recording subsequent measurements.

The [10K report](measurements/p0-lance-10k-scale.json) and
[validation record](measurements/p0-lance-validation.json) are bound by the
[signed aggregate](measurements/p0-lance-signed.json), using the continuation
audit's pinned public key. Source hashes match `28a5a94`. A million-vector run
is in progress; no result from that run is claimed yet.
