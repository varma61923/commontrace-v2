# Exact graph and consolidation improvements

Implemented against baseline `c8ce5c792916c10e23b0618c59259960294b7bcc`
on 2026-10-05. The candidate was the working tree over that revision; the
four owned production source digests and all samples are recorded in
[graph-phase6-results.json](graph-phase6-results.json). These are local
implementation measurements, not official answer accuracy or a competitor
comparison. No models, embeddings, external APIs, or new dependencies are used.

## What changed and why

`commontrace/graph.py` shares one indexed raw generation across current,
valid-time, and observed-time queries. Historical neighbors and bounded BFS
check temporal eligibility only on incident edges; they no longer reread and
decode the entire graph or construct all-node adjacency for each distinct date.
Returned order, weights, relation filtering, direction, temporal eligibility,
hop distances, and existing caller edge limits are preserved.

Cold snapshot capture shares the mutation lock when available. The generation
identity includes device, inode, modification and change times, and size for
both source files; a preserved modification time and byte count cannot hide
an atomic replacement. The captured identity is checked around the read and
again before cache publication. Three continually changing reads fail with an
explicit retry error. Permission-denied/read-only-mount lock creation
uses optimistic identity checks; this fallback does not claim an inter-file
transaction against a writer with different filesystem permissions. Public
edge/version-chain outputs are detached deep copies, so caller mutations
cannot corrupt another query's cached evidence.

Current validity now evaluates both scheduled starts and future closures at one
captured request instant: a move effective in 2030 cannot become current in
2029. Neighbor, BFS, and export requests share their instant across all edges.
Current export views invalidate at the earliest future start, closure, or TTL
boundary across all edges, including presently inactive ones, and rebuild if
the wall clock moves backwards. File stamps alone cannot detect time passing.
Explicit historical views retain fixed-time caching and recorded-close rules.

`commontrace/communities.py` constructs the same symmetric adjacency directly,
removing a pair-to-signal set that was discarded immediately. Identical member
groups from different tags, traces, or entities expand their clique once.
Distinct groups and every edge are retained. Sorted adjacency and seeded
label-propagation code are unchanged. Current community summaries exclude
future, ended, expired, forgotten, and inactive facts. A large distinct clique
still has quadratic edge storage: this change makes no linear-scale claim and
silently caps nothing.

`commontrace/hierarchical.py` keys token memoization with the actual statement
as well as ID/revision. Imported or stale matching IDs/revisions from another
root cannot inject that root's token set into retrieval. Search locally checks
the current validity window when `as_of` is absent, while historical `as_of`
and `show_expired` retain their separate meanings. Administrative `list_facts`
status listing is unchanged. Malformed reversed validity windows are skipped,
matching the documented unreadable-row behavior rather than crashing all
facts retrieval.

`commontrace/observations.py` gives identical statements in distinct scope sets
distinct IDs using structured JSON and a separate `obs-scoped-` namespace;
an unscoped statement containing a crafted NUL boundary suffix cannot collide
with a scoped derived ID. It records `scopes` plus `source_fact_ids` alongside trace evidence.
Optional scope filtering retains the existing global/unscoped visibility rule;
default unrestricted loading remains an administrative view. Old unscoped IDs
and legacy observation rows remain readable. Consolidation includes only
reinforced sources currently valid and unexpired at the requested time;
recorded/created time does not override effective validity. Malformed infinite
proof counts cannot crash loading or boosting. Materialized observations still
require reconsolidation after source changes; this is not an automatic
invalidation service.

## Local measurements and practical limits

Five measured runs each, CPython 3.12.14, Linux x86_64. Shared CPU load existed;
medians are workload observations rather than latency guarantees. No model
token/call budget changes were involved.

| Exact workload | Baseline median | Candidate median | Observed change |
|---|---:|---:|---:|
| Warm 20-date neighbor batch, 20,000 edges, target degree 8 | 4,492.010 ms | 0.344 ms | ~13,076× faster |
| Cold first graph query over the same source | 92.154 ms | 122.495 ms | ~32.9% slower |
| Paired cold first query + following 20 distinct-date queries | 4,582.029 ms | 122.837 ms | 37.30× faster |
| Adjacency construction, 500 members, 24 identical shared-tag membership groups | 463.679 ms | 33.757 ms | 13.74× faster |
| Community construction traced Python peak allocations | 57,711,984 B | 18,648,240 B | 3.09× lower |

The cold-inclusive row takes the median of five paired timing sums, each
containing the first current query and the following twenty distinct-date
queries. It includes index setup and excludes fixture/date construction,
HTTP transport and model work. Individual sums are retained in the JSON.

The historical gain comes from eliminating repeated whole-corpus decoding and
global filtering; it applies to warmed low-degree queries over distinct dates.
It is not a total system, official memory-quality, or universal 20× gain.
Cold first-query cost grows because all raw edge indexes are built once;
one-shot current-only consumers may favor a lighter cold path in a future
controlled experiment. The adjacency measurement benefits from redundant
group membership; its gain depends on that workload. `tracemalloc` measures
Python allocations, not process RSS or durable storage. No 1M/10M-token
answer-quality improvement is established by these synthetic runs.

Every baseline and candidate run produced the same full response hash for its
workload. The community hash includes both adjacency and resulting labels.
Those outputs and all individual timing samples are retained in the JSON.

## Reproduce and verify

Create an independent checkout at the pinned baseline outside the working
source tree, then run the same scripts against that checkout and the candidate:

```sh
git worktree add --detach ../commontrace-graph-phase6 c8ce5c792916c10e23b0618c59259960294b7bcc
python research/graph-phase6-profile.py ../commontrace-graph-phase6 > graph-before.json
python research/graph-phase6-profile.py . > graph-after.json
python research/graph-phase6-communities-profile.py ../commontrace-graph-phase6 > communities-before.json
python research/graph-phase6-communities-profile.py . > communities-after.json
python -m pytest tests/test_communities_phase6.py tests/test_graph_phase6.py tests/test_observations_phase6.py tests/test_graph_indexes.py tests/test_knowledge_graph.py tests/test_observations.py tests/test_hierarchical_facts.py tests/test_graph_viz.py tests/test_communities.py tests/test_perf_scale.py -q
```

The measured execution used the baseline checkout at
`/workspace/references/commontrace-graph-phase6` and the candidate at
`/workspace/commontrace-v2`. Both profilers use temporary isolated stores and
leave repository memory untouched. Defaults are five runs; graph defaults are
20,000 edges and 20 dates, community defaults 500 members and 24 groups. Graph
cold initialization is timed separately from the warmed historical batch.
Community allocation profiling is separate from the uninstrumented timed runs.

Focused verification: **88 tests passed in 7.33 seconds**, including 30 new
regression cases. Ruff passed for every owned source, profiler, and test file.
New tests exercise temporal eligibility against a reference, incident-edge
work counts, single-generation reads across dates, publication races,
same-byte-count/matched-mtime replacement, permission-denied/read-only-mount
snapshots, unexpected I/O propagation, deep mutation
isolation, root identity, scheduled current-versus-historical updates, one
request instant across a clock transition, clock expiry and rollback, all-edge community
equivalence, source validity, distinct scoped provenance and boundary collision
resistance, legacy observations,
malformed counts/windows, and imported token-cache collisions.

The observation fixture freezes fact creation at its already-fixed evaluation
`NOW`. Previously evaluation ran on 2026-10-04 while source creation used the
2026-10-05 wall clock, creating unintended future facts. Existing assertions
are preserved; dedicated tests now cover genuinely future and expired sources.
The performance fixture's private-cache assertions now inspect the full
generation index instead of the replaced all-node adjacency cache; behavioral
equivalence and write-invalidation assertions are preserved.

## Research connection

These changes independently apply mechanisms discussed in the pinned audit:

- [Graphiti search](https://github.com/getzep/graphiti/blob/b7fc30f2a1e288266760640164a37bdb7d1d0f28/graphiti_core/search/search.py)
  and [Zep temporal graph paper](https://arxiv.org/abs/2501.13956): bounded
  traversal, distinct validity/observation time, and communities that preserve
  evidence. Scope identity and generation safety are CommonTrace-specific
  engineering fixes; an index is not a causal graph.
- [Mem0 implementation](https://github.com/mem0ai/mem0/blob/abb81c88e1f738a8117d8293530fbc31a5ef8fd9/mem0/memory/main.py):
  reuse stored text representations instead of recomputing query-independent
  work. CommonTrace's memo now validates content identity, not a trusted
  imported revision alone.
- [Hindsight retain/recall/reflect research](https://arxiv.org/abs/2512.12818):
  retain source-grounded observations and time relevance rather than treating
  a condensed statement as timeless and unscoped.
- Raghavan, Albert, Kumara,
  [Near linear time algorithm to detect community structures in large-scale networks](https://arxiv.org/abs/0709.2938):
  CommonTrace's existing label propagation is unchanged. The optimization is
  in redundant clique construction, not a replacement of the algorithm.

Competitor implementation licenses were inspected in
[algorithm-audit.md](algorithm-audit.md). Graphiti and Mem0 are Apache-2.0;
Hindsight is MIT. No competitor source was copied, including Honcho's AGPL
implementation. No official benchmark dataset, category, judge, or scoring
code informed these exact-output optimizations.
