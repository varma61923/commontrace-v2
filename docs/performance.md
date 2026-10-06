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
