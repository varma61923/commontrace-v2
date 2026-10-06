# Verified listing reuse for local retrieval

This change removes repeated sorting, source-fingerprint allocation and duplicate cold scans while preserving full filesystem validation on every request. Initial measurements over 6,400 lessons and 100 distinct queries produced a scoped gateway p95 of 27.70 ms and direct local retrieval p95 of 22.81 ms. They concern model-free local handlers, excluding TCP/TLS and external rerankers. They do not establish a universal sub-30 ms service guarantee.

| Workload | Baseline p50 / p95 | Candidate p50 / p95 |
| --- | ---: | ---: |
| Scoped gateway recall, 3,200 eligible lessons | 29.03 / 62.85 ms | 23.57 / 27.70 ms |
| Direct local retrieval, 6,400 lessons | 30.57 / 73.93 ms | 19.25 / 22.81 ms |
| Strong source listing | 15.12 / 31.76 ms | 12.93 / 16.60 ms |

Baseline revision is `598409064558cec0ea1f5642381dcfb16d3fe7ff`. Both runs performed 100 authoritative directory scans for the 100 measured listing calls, and complete scoped responses have the same SHA256. The worker was shared with other development activity, so baseline tail variability limits the strength of speedup claims. Final integrated serial measurements should take precedence over these initial samples. The single cold scoped request measured 673.87 ms before and 754.61 ms after; it is not a cold p95 measurement or evidence of a cold-time gain. [Individual samples and environment](latency30-serving.json) preserve these limits.

## Implementation and correctness

`lesson_cache._scan_listing` still enumerates all lesson filenames and checks device, inode, nanosecond modification time, nanosecond change time and size for each file. An unchanged source reuses its prior immutable identity tuple. If all identities and membership match, the previous sorted listing and fingerprint are reused, avoiding repeated sorting and downstream tuple comparisons. Changed or deleted files produce a new generation. No timer, directory timestamp shortcut or filesystem notification replaces source checks.

Verified listings use an LRU capped at eight roots, 32,768 total lesson entries and 4 MiB of encoded path bytes. Python object and identity overhead is additional but bounded by the entry count. Oversized listings remain readable without retention. Scanning happens outside the cache lock. Public `listing` returns a plain immutable tuple; internal identity maps are read-only and snapshot attributes reject reassignment. A fork callback resets the inherited cache lock, cached listings and request scan scope, preventing a child from waiting on a vanished parent thread or inheriting an obsolete request snapshot.

The gateway's `_cached_active` lookup and cache-miss metadata loading share `one_scan`, so cold and changed requests no longer scan every source twice. Each warm request still performs its own verified scan. Phase 6 temporal eligibility, scope filtering, body-generation checks and generated-cache migrations remain active.

## Rejected notification experiment

We tested synchronous Linux inotify invalidation with directory and per-inode watches, descriptor-bound inode identity, post-scan draining, watch budgets and authoritative fallback. It accelerated unchanged lookups, but memory-mapped writes can change contents without producing usable notifications. Trusting an empty notification queue would therefore weaken source and scope freshness. The notification helper and its tests were removed; none of its attractive timing numbers describe shipped code. The retained mmap regression verifies a same-size scope change with its original modification time restored.

The supplied roadmap's sub-30 ms budget is a proposed target rather than reproduced evidence. This work uses no ANN library, model API, embedding service or new dependency. It addresses filesystem overhead without approximating retrieval or changing judges.

## Reproduce and validate

```bash
python research/profile_latency30_serving.py /path/to/baseline --lessons 6400 --requests 100 --distinct
python research/profile_latency30_serving.py /path/to/candidate --lessons 6400 --requests 100 --distinct
python -m pytest tests/test_latency30_serving.py tests/test_lesson_cache.py tests/test_serving_phase6.py tests/test_perf_index.py tests/test_gateway.py tests/test_gateway_pagination.py tests/test_workbench_serving_cache.py tests/test_mcp_fusion.py -q
```

The targeted suite passed 133 cases in 7.50 seconds. The eight new cases cover verified generation reuse with a scan on every request, deletions, mmap scope changes with restored modification times, public/internal immutability, entry/path-byte limits, root eviction, cold/changed/warm single scans and a child forked while another thread holds the cache lock. The final fork case also begins inside `one_scan` and verifies that the child sees a newly created lesson. Ruff and whitespace checks passed.
