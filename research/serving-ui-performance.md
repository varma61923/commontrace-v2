# Fresh source data with faster console polling

Measured against `8846feaccf94f6ad9dc37787c3c1f8fb63acdcca`, the repeated paginated lesson handler is 32.6× faster for active lessons and 100.7× faster for review drafts in the local workloads below. These improvements concern repeated UI requests with unchanged source content. They do not establish a 20× gain for every operation, memory-answer accuracy, or superiority over competitors.

| Workload | Baseline warm median | Candidate warm median | Ratio | YAML reads, baseline → candidate |
| --- | ---: | ---: | ---: | ---: |
| 1,000 active lessons, page of 25 | 106.132 ms | 3.260 ms | 32.56× | 2,000 → 0 |
| 1,000 active lessons and 25 review drafts, page of 25 drafts | 582.761 ms | 5.786 ms | 100.72× | 2,050 → 0 |

Both workloads returned identical response bodies, including total counts, ordering, gate results and nearest active lesson diagnostics. Five measured warm requests follow one measured cold request. Cold handler times were 299.970 → 207.140 ms for the active page and 831.240 → 739.561 ms for the review page. The cold-source read count halves because page and total now share one listing; content parsing and review diagnostics still happen on first access. Timing variability on the shared development worker is recorded as individual samples in [the results JSON](serving-ui-performance-results.json).

These measurements run the actual `Gateway.handle` route, including telemetry, parsing, diagnostics and response serialization. They exclude TCP, TLS and browser rendering. Synthetic lesson contents exercise ordinary frontmatter, scopes, provenance, source-trace counts and exact redundancy diagnostics. No model or embedding calls are involved. Corpus creation is outside request timing. The fixture is a performance regression workload; it contains no official question/answer data or judge modifications.

## What changed

`commontrace/workbench.py` retains complete parsed lesson frontmatter and bodies in a process-local LRU. Each request freshly lists source files and checks device, inode, nanosecond mtime, nanosecond ctime and size. A same-size overwrite with a restored mtime and an atomic file replacement invalidate cached parsing. Source deletion removes the item from the returned listing. Cold parsing runs outside the cache lock; source identity is rechecked before publishing, so an older reader cannot overwrite a newer cached source.

The parsing cache holds at most 4,096 entries and 16 MiB of serialized frontmatter/body content; Python object and key overhead is additional and bounded by the entry count. Oversized lessons remain readable but bypass retention. Detail/edit callers receive deep copies of nested metadata. Listing/counting use internal read-only views and copy mutable values into their public output. The source parser remains the existing safe frontmatter implementation. There is no persisted cache file or new dependency.

`lesson_page` returns the page and total from one scoped listing. Gateway pagination now uses that operation instead of reading the corpus independently for each result. Active-text construction is skipped when the requested page has no review drafts. The compatibility `list_lessons` and `count_lessons` APIs remain available.

A separate 512-entry LRU retains passive review diagnostics. Its key contains hashes of the complete draft frontmatter, exact body and the scoped active `(slug, comparable text)` corpus. Active-body, status and scope changes therefore invalidate the relevant diagnostics. The underlying functions, `draft_quality.gate_failures` and `redundancy.closest`, are pure content checks. Returned diagnostics are copied. **Authorization, approval decisions, separation-of-duties policy, history and clock-based eligibility are not cached.** CLI approval still runs every production gate itself. Scoped filtering and gateway authorization occur on every request before returning cached content.

`commontrace/ui/app.js` now polls the data that the visible page uses. For example, a warm review page makes two requests (status and lessons), compared with five previously; the memories and agents reports are no longer requested by that page. Capability discovery happens on connection and at least once per minute, scoped to the active credential. Replacing or rejecting a credential immediately clears retained source data, selections, notices and update timestamps and forces a fresh page render, including while an edit control has focus. A replacement credential always refetches capability information. Requests for live source data still run at the existing cadence. A generation/token/route guard prevents an older overlapping request from overwriting newer page data or clearing a replacement credential after a delayed authentication failure. Existing shared tokens, navigation, accessible controls, theme behavior and no-store HTTP policy are retained.

## Reference implementation study

This implementation was written independently; no competitor source was copied.

* **Hindsight**, MIT, `f7dd3f4fd7420f7beec60c32c965e5e5cf7be066`: [`bank_info_cache.py`](https://github.com/vectorize-io/hindsight/blob/f7dd3f4fd7420f7beec60c32c965e5e5cf7be066/hindsight-api-slim/hindsight_api/engine/bank_info_cache.py) makes tenant-qualified keys, invalidation and exclusion of authorization/routing data explicit. CommonTrace adopts those correctness principles; source file identity provides fresh reads across processes instead of introducing a TTL for source content.
* **Letta Code**, Apache-2.0, `f898fda60932b34ddbcfd389ea414515b0a5d272`: [`device-status-cache.ts`](https://github.com/letta-ai/letta-code/blob/f898fda60932b34ddbcfd389ea414515b0a5d272/src/websocket/listener/device-status-cache.ts) scopes duplicate suppression to transport and agent/conversation identity. The useful idea is to eliminate repeated unchanged work without sharing mutable identity-sensitive state. CommonTrace applies bounded identity-qualified parsing and content-addressed diagnostics.
* **Mem0**, Apache-2.0, `abb81c88e1f738a8117d8293530fbc31a5ef8fd9`: [`use-api-query.ts`](https://github.com/mem0ai/mem0/blob/abb81c88e1f738a8117d8293530fbc31a5ef8fd9/server/dashboard/src/hooks/use-api-query.ts) separates enabled route queries from refetch behavior. CommonTrace keeps its dependency-free renderer and requests sources according to the visible route. Its overlapping-request guard is implemented independently.
* **Graphiti**, Apache-2.0, `b7fc30f2a1e288266760640164a37bdb7d1d0f28`: [`cache.py`](https://github.com/getzep/graphiti/blob/b7fc30f2a1e288266760640164a37bdb7d1d0f28/graphiti_core/llm_client/cache.py) describes the security benefit of JSON data rather than deserializing executable pickle content. CommonTrace uses its existing safe YAML parser and in-memory dictionaries; there is no pickle deserialization or additional disk cache.

## Reproduce and verify

Run the same helper against independent baseline and candidate checkouts:

```bash
python research/profile_workbench_serving.py /path/to/baseline --lessons 1000 --runs 5
python research/profile_workbench_serving.py /path/to/candidate --lessons 1000 --runs 5
python research/profile_workbench_serving.py /path/to/baseline --lessons 1000 --reviews 25 --runs 5
python research/profile_workbench_serving.py /path/to/candidate --lessons 1000 --reviews 25 --runs 5
python -m pytest tests/test_gateway.py tests/test_gateway_pagination.py tests/test_workbench.py tests/test_workbench_serving_cache.py tests/test_ui_refresh.py -q
node --check commontrace/ui/app.js
```

Seven new serving-cache cases cover fresh pagination, rejected authentication after a warm request, same-size/restored-mtime edits, replacements, deletions, nested metadata and diagnostics isolation, active-corpus scope/status/content changes, entry/content budgets, oversized source bypass, concurrent roots and older-reader publication. Twelve frontend cases execute the actual polling controller in Node with deterministic asynchronous responses: all eight route query sets, capability freshness, out-of-order route polls, warm credential replacement, rejected current credentials and delayed errors from replaced credentials. Node cases skip when the execution environment does not provide Node; production Python dependencies remain unchanged.

The directory scan still takes linear time in the number of lesson files, and changed review inputs require running the actual gates again. The recorded ratios are gains for this bounded, unchanged-corpus polling workload rather than universal latency multipliers.
