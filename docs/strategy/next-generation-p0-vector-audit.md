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
audit's pinned public key. Source hashes match `28a5a94`.

The [million-vector report](measurements/p0-lance-million-scale.json) completed
on the same host with concurrent validation. Its 20 paired queries measured
exact p95 487.89 ms and ANN p95 13.04 ms. Mean recall@10 was 0.91 and minimum
recall 0.70, below the existing 0.97 vector-recall target. Loading took 184.81
seconds and index construction 51.80 seconds. The diagnostic process exited 0;
that records workload completion and does not certify a release gate. Defaults
need recall tuning before this result can support deployment. Reproduce with:

```sh
python -m benchmarks.vector_scale_bench --engine lance --n 1000000 --dim 384 \
  --queries 20 --top-k 10 --clusters 2000 --seed 61923 --batch 10000 \
  --out /path/to/lance-million.json
```

The report's source hashes match `4dd4a38`. The 10K and million-vector workloads
use different cluster counts and batch sizes; do not treat their difference as
a controlled scaling experiment. The million-vector exact/ANN pair uses the
same stored vectors and queries. Neither run includes full hybrid retrieval.

## Recall remediation plan

Retain a fresh matching million-vector store and compare query settings on that
same index against exhaustive cosine neighbors. Sweep scalar-quantization
refinement and HNSW exploration independently before changing product defaults.
Lance's documented `refine_factor` reranks candidates using the original vectors;
`ef` increases graph exploration. Keep scope prefilters, background maintenance
and new-fragment visibility intact. Record individual queries and repeated rounds,
then check additional held-out queries after selecting a setting.

Before implementation, add a real-engine regression that checks returned cosine
scores against original vectors after quantized ANN maintenance. Quantized scores
can otherwise move relative relevance floors even when candidate recall is high.
Run the existing owner, generation, deletion and fresh-fragment contracts again.
The unchanged acceptance targets remain recall at least 0.97 and local p95 below
50 ms; vector-only measurements still cannot establish the full hybrid gate.

The matching retained-store baseline measured recall 0.905. Three randomized
rounds on that same index measured recall 0.965 with refinement 2 and 0.98 with
refinement 4 or 8. Increasing `ef` alone reached 0.94. Select refinement 4 without
changing graph exploration: it recovers the measured recall with fewer original
vector reads than refinement 8. Validate this choice through the product API,
including additional queries withheld from the sweep.

The committed product API measured recall 0.98 on the tuning queries and 0.995
on 20 held-out queries in each of three rounds. Tuning-query p95 was 24.03,
16.35 and 14.62 ms; held-out p95 was 21.16, 22.14 and 16.60 ms. Minimum per-query recall
was 0.70 and 0.90 respectively: the mean target does not guarantee every query.
The cosine-score regression failed before refinement and all five Lance tests
passed afterward. CI's optional-engine job now includes the scale-harness tests.
These are synthetic component results on one retained index with concurrent
application validation, not model quality or full hybrid acceptance evidence.

Add an explicit component-check option to the existing scale harness and a
`reproduce.sh --lance-vector` entry point. Tests first must reject a smaller store,
latency equal to 50 ms, recall below 0.97 and non-finite results. Preserve the
default diagnostic exit semantics; the explicit check must exit nonzero on a
component miss and cannot certify full hybrid retrieval.

The five component-boundary cases failed before the gate helper existed, then
all ten Lance contract, scale and gate checks passed. Reproduce the current
product setting and enforce the component thresholds with an environment that
has the declared `vector-lance` extra installed:

```sh
./reproduce.sh --lance-vector /path/to/output
```

This entry point returns nonzero when scale, latency or mean recall misses. The
scale harness without `--check-lance` remains a diagnostic and returns zero on
workload completion. Both reports explicitly leave full hybrid acceptance open.

The fresh reproducer completed on `3a72da1` and returned 0 with all three explicit
component checks true. Its 20 paired synthetic queries measured ANN p50 14.38 ms,
p95 19.69 ms, mean recall@10 0.995 and minimum recall 0.90; exact p95 was
462.97 ms. Loading took 181.77 seconds and index construction 54.22 seconds.
This separately built index does not establish a controlled before/after effect;
the retained-index randomized sweep above provides that comparison.

The [signed refinement manifest](measurements/p0-lance-refinement-signed.json)
binds the retained baseline, randomized sweep, committed product validation,
fresh million-vector report and validation checks using the continuation audit's
pinned public key. The sweep and product reports contain the diagnostic source.
To reproduce the same-index experiment, make an isolated checkout of `eb3b528`,
run its documented million-vector command with `--keep`, and pass that report to
the sweep diagnostic. Run the product diagnostic from the current checkout using
the retained baseline and full sweep report. Neither diagnostic mutates or drops
the retained vector index. The fresh reproducer cleans its own temporary store.
