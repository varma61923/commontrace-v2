# Demand-driven graph reads and the 30 ms goal

Implemented on 2026-10-06 against graph source at
`598409064558cec0ea1f5642381dcfb16d3fe7ff`. The candidate base
`ccd131d4b76bf187c94f15d58426f14f996ae293` has identical baseline production
graph code. Candidate source SHA-256:
`8b5f6d0fc57da67c38a82f6ae120109da7b2963882b5a809776f690e201b70d1`.
All samples, hashes, and environment details are in
[latency30-graph.json](latency30-graph.json).

The demonstrated improvement is that node-only requests no longer decode or
temporally filter unrelated edges. Their sampled cold-cache p95 is below
4 ms in these fixtures. Warm bounded graph requests are below 0.2 ms.
Cold raw JSONL edge retrieval remains above 30 ms; this work does not establish
a universal sub-30-ms request or end-to-end service guarantee.

## Implemented changes

`commontrace/graph.py` now has a separate node-generation cache used by entity
extraction, phrase construction, and version chains. A node snapshot includes
the root identity and all five source-file identity fields: device, inode,
mtime, ctime, and size. It shares the existing graph mutation guard, checks
identity around a read, bounds retries to three, and revalidates before
publication. Permission-denied/read-only filesystems retain the existing
optimistic fallback; unexpected lock I/O errors propagate. Full graph reads
reuse the same node generation, avoiding a second parse. Public version-chain
outputs remain detached deep copies.

Phrase-index keys now depend on the captured node generation rather than
unrelated edge changes. A stale phrase reader cannot publish under a new
generation. The original whole-term name/ID/alias matching, first-request scan,
and forgotten-node eligibility remain unchanged.

Full snapshots eagerly build only incident-edge lists. Timestamp, relation,
source/relation, global interval-order, and version-chain indexes materialize
when requested. Repeated timestamp strings share parsed moments within that
generation. Entity intervals and timelines inspect only candidate timestamps;
global interval queries retain complete cached arrays and sorted ordering.
Combined entity/relation queries preserve the baseline smaller-pool choice,
including high-degree entities with a rare relation. This avoids a discovered
regression in a prototype that always scanned the entity pool.

Current/scheduled validity, historical `as_of`, observed-time `known_at`, single
captured request time, expiry/closure/start cache deadlines, root isolation,
same-size/matched-mtime replacement detection, and existing caller hop/edge
limits are unchanged. No stored memory, model choice, context budget, ranking,
official judge, or dependency changes were needed. No persistent artifact is
created and graph mutations do no new preparation work.

## Measurements, including regressions

Fixtures have **520 graph nodes**, target degree 8, and either **20,000 or
100,000 graph edges**. These are graph edge counts, not BEAM token scales.
Round-robin unrelated edges and repeated dated fields are deterministic.
Five cold-cache calls and 50 warm calls per API were measured. Cold means Python
graph caches were cleared; source files remained in the OS page cache. Import,
process startup, fixture construction, HTTP, model inference, embeddings, and
reranking are outside the measurements. Shared machine CPU activity existed.
Reported p95 is the nearest-rank sample statistic, not a validated production
SLO or a tail-confidence bound.

| API | Cold p50, 20k baseline → candidate (ms) | Cold p50, 100k baseline → candidate (ms) | Warm p95, 100k baseline → candidate (ms) |
|---|---:|---:|---:|
| Entity extraction | 140.955 → 3.047 | 872.193 → 2.576 | 0.140 → 0.071 |
| Version chain | 102.985 → 2.114 | 743.750 → 2.041 | 0.088 → 0.098 |
| Current neighbors | 116.701 → 91.698 | 719.837 → 631.878 | 0.019 → 0.017 |
| Historical neighbors | 118.244 → 97.482 | 718.816 → 673.212 | 0.018 → 0.016 |
| Bounded multi-hop | 159.558 → 89.289 | 749.939 → 718.325 | 0.071 → 0.072 |
| Entity interval | 120.180 → 86.574 | 682.427 → 763.104 | 0.111 → 0.169 |
| Entity timeline | 115.683 → 88.560 | 696.986 → 771.524 | 0.022 → 0.022 |

At 100k edges, node-only cold gains are approximately 339× and 364× because the
unrelated edge corpus is never touched. Those factors depend on fixed node
count and this edge-heavy workload. They are not whole-system speedups.
20k candidate cold p95 for those APIs is 3.657/2.198 ms; 100k is
3.126/2.162 ms. Graphs with many more nodes can have a higher node decoding cost.

Cold raw-edge interval/timeline regressed about 12%/11% in the initial 100k
matrix. A subsequent serial baseline-then-candidate run restricted to those
two APIs produced mixed results:

| Serial 100k repeat | Cold p50 baseline → candidate (ms) | Warm p95 baseline → candidate (ms) |
|---|---:|---:|
| Entity interval | 717.768 → 857.946 | 0.131 → 0.146 |
| Entity timeline | 670.243 → 588.385 | 0.022 → 0.019 |

No consistent improvement is claimed for every cold edge API. JSON decoding,
allocation/GC, and shared CPU dominate these requests; attributing the entire
regression to noise would be unsupported. Lazy indexing reduces unnecessary
derived work and allocation, but does not eliminate authoritative raw-source
loading. The fixture still requires decoding every edge on its first edge
lookup, so a strict cold sub-30-ms promise would be false. Small warm timing
regressions are retained in the table rather than hidden by aggregate gains.

Every baseline/candidate API output hash matches in the original two fixtures
and serial repeat. The measured combined interval filter uses entity only;
the later restoration of the smaller-pool combined-filter branch does not
affect those measurements. All raw samples are retained, including slower
runs. LLM and embedding calls are zero; durable storage and mutation work
are unchanged. No empirical process-RSS improvement is claimed.

## Verification and reproduction

**53 focused tests passed in 1.90 seconds**, including nine new regression
tests. Ruff and `git diff --check` passed. Existing graph tests cover exact
interval/timeline/version results, temporal boundaries, scheduled updates,
clock transitions/rollback, cache freshness, read-only behavior, deep mutation
isolation, and bounded traversal. New tests prove zero edge reads for node-only
APIs, lazy subsidiary construction, node-only publication races, same-byte-size
node replacement with restored mtime, stale phrase-reader rejection, shared
node generation reuse, read-only mounts, distinct timestamp parsing, and a
1,000-degree entity versus one rare relation. Two combined-filter calls inspect
two relation candidates total and zero entity candidates while retaining
temporal exclusion/inclusion.

```sh
git worktree add --detach ../commontrace-latency30-graph 598409064558cec0ea1f5642381dcfb16d3fe7ff
python research/latency30-graph-profile.py ../commontrace-latency30-graph > graph-before-20k.json
python research/latency30-graph-profile.py . > graph-after-20k.json
python research/latency30-graph-profile.py ../commontrace-latency30-graph --edges 100000 > graph-before-100k.json
python research/latency30-graph-profile.py . --edges 100000 > graph-after-100k.json
python research/latency30-graph-profile.py ../commontrace-latency30-graph --edges 100000 --operations entity_interval,entity_timeline > graph-repeat-before.json
python research/latency30-graph-profile.py . --edges 100000 --operations entity_interval,entity_timeline > graph-repeat-after.json
python -m pytest tests/test_graph_latency30.py tests/test_graph_phase6.py tests/test_graph_indexes.py tests/test_knowledge_graph.py tests/test_graph_viz.py tests/test_perf_scale.py -q
```

The measured baseline checkout was
`/workspace/references/commontrace-latency30-graph`; candidate was
`/workspace/commontrace-v2`. The scripts create isolated temporary stores and
never modify repository memory. Stop competing CPU workloads for deployment
latency measurement; the retained runs deliberately do not disguise shared
load as an isolated performance laboratory.

## Report proposals assessed

The uploaded report's PPR proposal addresses ranking, not cold raw-source
decoding. Personalized PageRank computes a distribution for a specified graph,
seed, transition model, and damping factor; it does not make relation importance
causal, ensure evidence sufficiency, or prove higher answer accuracy. It also
adds graph construction and iterative propagation work. No PPR change is
adopted without a separate controlled quality/latency comparison. Existing
bounded incident traversal is already well below 30 ms when warm.

A persistent, generation-validated incident-edge lookup could make a prepared
fresh-process edge query avoid whole-file parsing, but adds artifact security,
source consistency, schema/corruption validation, allocation limits, storage,
and preparation costs. Building it on every graph mutation would penalize
writes. It was considered and **not added** in this round. A future design
must show exact provenance, read-only behavior, stale-generation rejection,
and favorable ingestion/retrieval tradeoffs before adoption. The report's
unconditional sub-30-ms/SOTA claim is not supported by the current evidence.

The pinned competitor and paper grounding remains in
[algorithm-audit.md](algorithm-audit.md) and
[roadmap-assessment.md](roadmap-assessment.md). This round independently
implements query-independent-work reuse and query-adaptive indexing; no
competitor code is copied, and no competitor superiority is established.
