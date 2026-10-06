# Performance evidence

Production measurements compare pinned implementations on local CPU hardware.
They establish specific workload improvements, not universal 30 ms latency or
superiority over competitor answer quality. No inference service is used by
the measured request profiles.

| Operation | Baseline | Improved |
| --- | ---: | ---: |
| Warm HTTP recall, 6,400 lessons, p95 | 219.922 ms | 22.951 ms |
| Warm HTTP recall, 1,000 lessons, p95 | — | 3.394 ms |
| Warm HTTP health, p95 | 44.080 ms | 0.733 ms |
| Node-only graph extraction, 520 nodes / 100,000 edges, median | 872.193 ms | 2.576 ms |
| Cold mapped exact retrieval, 200,000 vectors / ten eligible passages | 24.836 ms | 0.703 ms |

HTTP measurements use baseline `ccd131d` and improved revision `7376495`,
three seeded fixtures, 150 warm requests per endpoint and persistent loopback
HTTP. Responses match exactly. Improved HTTP recall p99 is 28.262 ms and its
maximum is 30.172 ms. Cold 6,400-lesson fixture setup still takes approximately
1.23 seconds. The vector row compares baseline `c8ce5c7`; full raw graph reads
also exceed 30 ms and include documented regressions.

The improved code passed 4,492 core/end-to-end tests (34 skipped), independent
reviews and all 30 push/PR CI jobs. The preceding security changes passed
2,464 local PostgreSQL Hub tests; CI repeats that matrix on Python 3.10–3.12.

## Detailed archive and reproduction

One-off scripts and machine-specific results were removed from the current
source tree. Their complete evidence remains available at immutable revision
`7376495fb1140cb864a669d2ca9472dad0ce1020`:

- [Request samples, source hashes and validation](https://github.com/varma61923/commontrace-v2/blob/7376495fb1140cb864a669d2ca9472dad0ce1020/research/latency30-http.json)
- [Request reproduction helper](https://github.com/varma61923/commontrace-v2/blob/7376495fb1140cb864a669d2ca9472dad0ce1020/research/profile_latency30_http.py)
- [Graph measurements, including regressions](https://github.com/varma61923/commontrace-v2/blob/7376495fb1140cb864a669d2ca9472dad0ce1020/research/latency30-graph.md)
- [Implementation and security review](https://github.com/varma61923/commontrace-v2/blob/7376495fb1140cb864a669d2ca9472dad0ce1020/research/phase6-production-review.md)
- [Pinned competitor commits, licenses and papers](https://github.com/varma61923/commontrace-v2/blob/7376495fb1140cb864a669d2ca9472dad0ce1020/research/competitor-analysis.md)
- [Verified push CI](https://github.com/varma61923/commontrace-v2/actions/runs/37398752232)
- [Verified PR CI](https://github.com/varma61923/commontrace-v2/actions/runs/37398755791)

Recover the optional HTTP profiler without restoring the research directory:

```bash
mkdir -p scratch
git show 7376495fb1140cb864a669d2ca9472dad0ce1020:research/profile_latency30_http.py > scratch/profile_http.py
python scratch/profile_http.py . --lessons 6400 --runs 3 --requests 50
```

The maintained local-only evaluation helper lives in `benchmarks/local_eval.py`.
Official judges, the conversation harness and their regression tests remain
because the CLI and tests use them. Runtime modules, source memory, fixtures,
protocol schemas, SDKs, distribution metadata and deployment assets are retained.

## Concurrency and migration hardening

This revision compares against `fd5461115e1c680d0572cca498a547e39502d516`
on local Linux/Python 3.12.14. These are CPU/SQLite/PostgreSQL workload profiles,
not official answer-quality scores. No model or embedding service is used.
Clean timings disable allocation tracing; allocation figures come from separate
`tracemalloc` runs and exclude native SQLite/PostgreSQL memory. Concurrent test
load affects timings, especially tails.

| Workload | Baseline | Improved |
| --- | ---: | ---: |
| Schedule 50,000 yielding sync callbacks, eight workers, median | 1,056.89 ms | 95.17 ms |
| Same batch, peak traced Python allocations | 75.05 MB | 2.75 MB |
| Hydrate 100 traces with 20 votes and 20 relations each, median | 58.64 ms | 16.26 ms |
| Same hydration, peak traced Python allocations | 4.57 MB | 2.14 MB |
| Repair 100,000 facts in 1,000 belief chains, median | 508.97 ms | 409.63 ms |
| Legacy migration: 10,000 turns / 5,000 facts, peak Python allocations | ~18.8 MB | ~0.75 MB |
| Read a 650 KB Markdown file with 50,000 delimiters, median | 20.285 ms | 0.501 ms |

Batch and repair timings use five alternating baseline/candidate runs; hydration
uses 15 interleaved measured runs after two warmups; file reads use 100 warm
samples. Exact ordered outputs or response hashes match. Batch timings measure
scheduling only, not remote Hub throughput; ordinary tiny file reads remain
approximately 0.008 ms. Migration runtime remains approximately 3.9 seconds.

Cold retrieval now shares one build across eight simultaneous requests, instead
of building eight indexes. The cache retains at most four entries and a
conservative 64 MiB estimate; indexes above that budget remain correct but are
not retained. Accounting adds approximately 2.74 ms to a serial cold build of
2,000 lessons (8.90 to 11.65 ms); hot cache lookup adds approximately 0.44 µs.
These budgets limit retained caches, not concurrent callers or active builds.

Parsed frontmatter has a thread-safe 16 MiB retention budget and resets safely
following a fork. New files honor the current umask and directory permissions.
File-lock acquisition retries contention only, closes descriptors on failure,
and has a 30-second default deadline; `locked(path, timeout=0)` tries immediately.
The deadline does not limit time spent inside the protected block. Actual Windows
locking was not exercised locally; Windows error paths have simulated tests.

Verification targets for these changes:

```bash
python -m pytest tests/test_retrieval_cache_safety.py tests/test_hub_batch_bounds.py \
  tests/test_conversation_store_scaling.py tests/test_frontmatter_cache_resources.py \
  tests/test_frontmatter_lock_resources.py -q
# With a disposable PostgreSQL configured through HUB_TEST_DATABASE_URL:
python -m pytest hub/tests/test_hydration_projection.py -q
```

## Chronology insertion CPU cost

Common fact insertions now reuse the predecessor's maintained successor link,
reducing neighbor SELECTs from two to one. Earliest insertions still seek the
successor; owner/slot isolation, timestamp ties and historical evidence are unchanged.
An independent sorted-history oracle covers shuffled/tied/unknown timestamps,
source links and rollback.

Compared with commit `6029dfa51efe8b8508f8ca563d1ecb2283ef5d6c` on the same local
Python/SQLite environment, ten alternating pairs of 3,000 inserts in a transaction
gave the following medians measured with `time.process_time()`:

| Insertion order | Baseline CPU | Improved CPU | Ratio |
| --- | ---: | ---: | ---: |
| Increasing timestamps | 78.43 ms | 74.32 ms | 1.05× |
| Permuted timestamps | 83.99 ms | 79.08 ms | 1.06× |

Each run uses a fresh temporary store, one source turn and the same owner/slot.
The timed block calls `_insert_fact` 3,000 times within `write_txn`; timestamps
are zero-padded ordinals, either `i` or `(i * 7919) % 3000`. Variant order reverses
on alternating repetitions. Store creation and source insertion are excluded.
These measurements describe CPU cost; concurrent system load made wall-clock
latency inconclusive. They do not establish a general 20× speedup.

```bash
python -m pytest tests/test_conversation_ingestion_performance.py -q
```

## Repeated lexical ranking and provider isolation (2026-10-06)

Compared with `bf16603eddc6b220efcab95ce4a6912a88e79318`, seven alternating
baseline/candidate pairs each measure 300 requests per workload. The corpus
index is warm for both versions. Seed 61923 creates 12 description terms and
three tags per lesson from a vocabulary of 100 terms; these are synthetic
frequent-term queries with top-k 10, not an answer-quality benchmark. Repeated
requests use the same three-term query; distinct requests use seeded four-term
queries. The table reports the median of each trial's median.

| Lessons | Workload | Baseline median | Candidate median | Change |
| ---: | --- | ---: | ---: | --- |
| 1,000 | repeated | 0.189701 ms | 0.020581 ms | 9.22x faster |
| 1,000 | distinct | 0.287228 ms | 0.310674 ms | 8.2% slower |
| 10,000 | repeated | 1.852324 ms | 0.020150 ms | 91.93x faster |
| 10,000 | distinct | 2.605150 ms | 2.682975 ms | 3.0% slower |

All ordered result hashes match across versions and trials. The 50x target is
exceeded for repeated ranking at 10,000 lessons. It is not met for every
operation: distinct queries pay cache admission/materialization overhead, and
the measured regressions above are retained in this report. These measurements
exclude file loading, cold indexing, prompt rendering, network transport and
LLM inference. They do not compare CommonTrace against hosted competitor SLAs.

For predominantly unique workloads, use `COMMONTRACE_QUERY_CACHE=0` or
`rank_lessons(..., cache_results=False)`. The separate opt-out comparison is
included below; it removes admission work, though numeric-template
materialization still has a small cost versus the baseline implementation.

- [All paired timings, output hashes and source hashes](../benchmarks/results/runtime-2026-10-06/retrieval-comparison.json)
- [Comparison with query caching disabled](../benchmarks/results/runtime-2026-10-06/retrieval-disabled-comparison.json)
- [Existing local latency gate](../benchmarks/results/runtime-2026-10-06/local-latency.md)
- [Test, coverage and static-analysis validation](../benchmarks/results/runtime-2026-10-06/validation.json)
- [Scope, operational controls and remaining gaps](runtime-upgrade.md)

Reproduce from this checkout (Python 3.10+, core dependencies only):

```bash
git worktree add --detach ../commontrace-baseline bf16603eddc6b220efcab95ce4a6912a88e79318
python -m benchmarks.runtime_comparison --baseline-checkout ../commontrace-baseline \
  --trials 7 --runs 300 --output /tmp/retrieval-comparison.json
COMMONTRACE_QUERY_CACHE=0 python -m benchmarks.runtime_comparison \
  --baseline-checkout ../commontrace-baseline --trials 5 --runs 300 \
  --output /tmp/retrieval-disabled-comparison.json
```
