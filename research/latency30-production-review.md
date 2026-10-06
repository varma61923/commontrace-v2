# Production latency improvements toward 30 ms

This change extends published baseline
`ccd131d4b76bf187c94f15d58426f14f996ae293`. Its production code matches
`598409064558cec0ea1f5642381dcfb16d3fe7ff`; the later commit corrects a
Python 3.10 regression-test assumption without changing SQL security.
The second user-provided roadmap is assessed in
[latency30-report-assessment.md](latency30-report-assessment.md). Existing
competitor repositories, papers and license review remain pinned in
[competitor-analysis.md](competitor-analysis.md).

## Actual persistent HTTP requests

The final run executes the baseline and candidate serially after local agent
profiling finishes. It creates three seeded synthetic stores per workload,
then sends 50 warm requests per endpoint per store over a persistent HTTP/1.1
loopback connection. Timings include socket transfer, request handling,
eligibility, exact lexical ranking, source validation, body retrieval, event
logging and client JSON decoding. Recall rotates queries and requests three
results. No inference or embedding calls occur.

| Operation | Baseline warm p50 / p95 | Candidate warm p50 / p95 |
| --- | ---: | ---: |
| HTTP health, 6,400-lesson store | 43.958 / 44.080 ms | 0.467 / 0.733 ms |
| HTTP recall, 6,400 lessons | 75.863 / 219.922 ms | 20.107 / 22.951 ms |
| HTTP recall, 1,000 lessons | Not paired in this final run | 3.041 / 3.394 ms |

Every baseline/candidate response in the 6,400-lesson run has the same combined
SHA256. Candidate p99 is 28.262 ms; its slowest of 150 warm recalls is
30.172 ms. These are empirical local distributions, not a universal worst-case
promise or an SLA. All samples, hardware/Python information, source hashes,
the superseded contended capture and GC diagnostic are retained in
[latency30-http.json](latency30-http.json).

Cold recall setup still exceeds the target: median 1,232 ms for the new
6,400-lesson fixture and 79.6 ms at 1,000 lessons. Only three setup samples
are collected; they do not establish a production cold p95. Arbitrary large
document ingestion, raw graph-file decoding, network transit, model loading
and model inference also require separate budgets.

Reproduce against the named checkout paths:

```bash
python research/profile_latency30_http.py /path/to/baseline --lessons 6400 --runs 3 --requests 50
python research/profile_latency30_http.py /path/to/candidate --lessons 6400 --runs 3 --requests 50
python research/profile_latency30_http.py /path/to/candidate --lessons 1000 --runs 3 --requests 50
```

## Retained implementation

The gateway enables TCP_NODELAY through the standard request-handler flag,
removing delayed-ACK stalls between separately written headers and small JSON
bodies. Authoritative lesson scanning still checks every leaf's device, inode,
modification time, change time and size on every request. Only an equal verified
generation reuses sorted listings and fingerprints. Retention is bounded by
root count, file count and encoded path bytes; public listing values are plain
tuples. Cold/mutated gateway loads share one request-local scan, and forked
children reset the new listing-cache lock and inherited scan scope.

Eligibility filtering reuses immutable input tuples instead of allocating a
new tuple per accepted lesson. The output list remains separate, while its
frontmatter dictionaries retain the existing sharing behavior. A GC diagnostic
observed two generation-two collections taking 38 and 57 ms before this change
and none across 50 requests afterward. That diagnostic's after run overlapped
targeted tests; its latency values are not a separate speedup comparison.

Exact lexical ranking delays matched-term/result allocation until selection,
uses sparse candidate state for rare queries and preserves original corpus tie
order, floating accumulation, adaptive floors and optional adjustments. Masks
cover present query terms only, retain full query normalization and are limited
to 64 bits; larger queries use per-hit lists. All 540 complete profile answers
and the large-query differential tests match the previous scorer. Default warm
ranking median improves 7.23 to 2.99 ms on its separate workload. Tiny rare
queries incur a small fixed overhead, documented in
[latency30-ranking.md](latency30-ranking.md).

Node-only graph APIs no longer read unrelated edges. Strong node generations
share snapshots with full graph reads; timestamp, relation and version indexes
are built when requested. Combined entity/relation queries preserve selection
of the smaller candidate pool. Node-only cold p95 is below 4 ms on the tested
520-node stores containing 20,000 or 100,000 edges; warm graph p95 is below
0.2 ms. Cold raw-edge results remain mixed, including measured regressions, and
are reported completely in [latency30-graph.md](latency30-graph.md).

The notification-only listing experiment was rejected: Linux mmap writes can
change content and metadata without an inotify modification event. No watcher,
staleness interval or relaxed source-isolation rule ships. No new dependency,
competitor source copy, ANN approximation, context expansion or official judge
change is introduced. Detailed serving evidence is in
[latency30-serving.md](latency30-serving.md).

## Validation

Independent agents reviewed source freshness, scope/temporal behavior,
concurrent snapshot publication, fork cleanup, long-query allocation bounds,
exact scorer values and HTTP/TLS compatibility. Final focused checks pass with
no blockers. The frozen production passed **4,492 core/end-to-end tests**
(34 skipped, 412.22 seconds), **82 Python 3.10 focused tests** (one optional
skip), and all **35 new regression cases**. Full Ruff, medium/high Bandit,
JavaScript syntax and whitespace checks pass. The existing latency regression
gate measures 18.5 ms median at 6,400 lessons, with fitted alpha 1.03; it ran
alongside tests and is separate from the serial HTTP comparison. The preceding
production/security release also passed 2,464 real-PostgreSQL Hub tests; final
exact-head CI repeats Hub and cross-version checks. Machine-readable validation
is in the accompanying JSON; publication and final CI links are recorded in the
pull request.
