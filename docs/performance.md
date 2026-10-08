# Performance evidence

Production measurements compare pinned implementations on local CPU hardware.
They establish specific workload improvements, not universal 30 ms latency or
superiority over competitor answer quality. No inference service is used by
the measured request profiles.

## Conversation reliability and multilingual retrieval (2026-10-08)

Acceptance criteria: coverage uses emitted source text rather than hidden turns;
explicit allow-lists constrain assembly; multilingual tail evidence is quoted
within budget; indexed and streaming Unicode scores match; ASCII behavior and
canonical hashes/journals remain compatible. The local suite at `da763d0` passed
**5,688 tests, with 103 skipped**. All 35 strict typing targets, Ruff and generated
documentation checks passed; 73 additional MCP tests passed against the pinned
dependency environment. Model-dependent and unconfigured integration tests remain
among the skips.

The subsequent keyboard-focus correction passed 16 real Chromium contracts and
19 controller tests. Two new regressions reproduce the old route/form loss and
refresh focus loss using the previous production JavaScript, then pass with the
fix. No timing threshold or retry was relaxed.

The measured implementation is `da763d0b9d00942cdab6012d2f939ad1f6c40206`.
The complete LoCoMo evaluation uses 1,540 questions in categories 1–4 across ten
conversations, production redaction, lexical retrieval, no reranker or models,
alternating strategy order and disabled final-response caching. All 1,536
gold-bearing questions contribute to source metrics, including nine unresolved
annotations retained as misses. Source retention is not answer accuracy.

| Estimated budget | Legacy whole-source recall | Optional coverage-v1 | Legacy p50 / p95 | Optional p50 / p95 |
| ---: | ---: | ---: | ---: | ---: |
| 1,500 | 76.4257% | 75.9655% | 8.083 / 13.313 ms | 14.477 / 20.680 ms |
| 7,000 | 88.9260% | 88.9239% | 19.657 / 28.721 ms | 25.759 / 36.608 ms |

Conversation-cluster bootstrap intervals (2,000 draws, seed 0) for optional
minus legacy whole-source recall are [-0.9075, +0.0429] percentage points at
1,500 and [-0.1267, +0.1573] at 7,000. There is no demonstrated aggregate gain;
`legacy` remains the default. Mean estimated context sizes are 1,441.79 / 1,441.14
and 6,478.76 / 6,476.49 respectively. These are `ceil(characters/4)` estimates,
not provider token counts. Timings exclude ingestion, evaluation and inference.
The final multilingual implementation reproduces the pre-change English source
recall and context-size aggregates exactly.

Reproduce with the official `snap-research/locomo` `data/locomo10.json` file:

```bash
python -m benchmarks.conversation_retrieval --dataset locomo \
  --data /tmp/commontrace-locomo10.json --budgets 1500,7000 --seed 0
```

Dataset SHA-256: `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`.
Implementation checksum emitted by this run:
`bcc514e932056864f12b66d50132ce95df5dc914c6feafbda76478dd743a14e0`.
Python 3.12.14 / SQLite 3.53.1. Comparing with hosted Mem0's reported scores
requires matched answer/judge models, actual tokenizer accounting and service
access; no competitor benchmark has been beaten by this evaluation.

Two isolated CPU/SQLite profiles verify performance changes on the same runtime:

| Component / workload | Reference median | Current median |
| --- | ---: | ---: |
| Unicode BM25: 10,000 units, ten matches | 125.085 ms streamed | 0.785 ms indexed |
| Broad assembly: 10,000 ranked turns | 16.108 ms before set hoist | 0.773 ms |
| Broad assembly: 20,000 duplicate rank entries | 29.824 ms before set hoist | 1.030 ms |

The Unicode fixture has 10,000 `Agent` messages: `麒麟缓存容量记录。` for ordinals
divisible by 1,000, otherwise `普通维护状态记录。`, followed by `指标编号{i}。`.
Seven alternating indexed/streaming queries for `麒麟缓存`, limit 20, return
identical ordered scores. Initial index build takes 489.015 ms. Source bodies are
not scanned by the warm indexed arm; statistics still scan Unicode document
lengths. These figures measure the BM25 component, not complete recall.

Assembly uses a real store with 50 sessions of 200 messages, a 400-token budget,
100-token excerpts, broad mode, and no profile, summary, neighbors or models.
The control reverses only the two-line membership-set hoist. Seven alternating
warm pairs at pools 200 / 2,000 / 10,000 cover ordinary, duplicate and scoped
rankings: all 63 pairs preserve context, IDs, token counts, withheld sources,
emitted text and selection diagnostics exactly. Assembly source checksum:
`4a894de10c75d2b813a38386f7db1b0bb9ac0e351eaa934aaf164b1c4898b76e`.
Neither component profile establishes a universal latency or speed multiplier.

## Earlier measured operations

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
git show 7376495fb1140cb864a669d2ca9472dad0ce1020:research/profile_latency30_http.py > /tmp/commontrace-profile-http.py
python /tmp/commontrace-profile-http.py . --lessons 6400 --runs 3 --requests 50
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
the measured regressions above are retained in this guide. These measurements
exclude file loading, cold indexing, prompt rendering, network transport and
LLM inference. They do not compare CommonTrace against hosted competitor SLAs.

For predominantly unique workloads, use `COMMONTRACE_QUERY_CACHE=0` or
`rank_lessons(..., cache_results=False)`. An opt-out comparison can be reproduced with the command
below; disabling caching removes admission work, though numeric-template
materialization still has a small cost versus the baseline implementation.

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
