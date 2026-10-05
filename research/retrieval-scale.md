# Generation-safe disk-backed exact retrieval

The change builds on `8846feaccf94f6ad9dc37787c3c1f8fb63acdcca`.
It removes repeated SQLite text/vector decoding for corpora that exceed the
128 MiB retained vector-matrix budget. Retrieval remains exact; no approximate
candidate search, additional model call, benchmark-specific answer or judge
change was introduced.

## Implementation

`commontrace/conversation/vector_index.py` stores one immutable NumPy snapshot
per source space/model, with float16 passage vectors, integer passage/turn ids,
content hashes and a format/source-generation header. SQLite owns the raw
evidence and generation. Loading validates format, dimensions, source identity,
generation, authoritative passage count and increasing positive passage ids
within the caller's consistent read snapshot. Invalid/incomplete files are
cache misses and are rebuilt from source evidence.

Oversized searches retain read-only mappings instead of reconstructing vectors
from SQLite on every recall. Each scoring batch still converts only 1,024 rows
and evaluates at most 16 query facets. The established scorer/top-k ordering and
independent sparse/dense fusion are unchanged. Small indexes retain their
existing resident matrices.

Warm filtered searches use the indexed SQLite passage lookup and binary-search
its sorted ids into the current matrix, instead of iterating over every cached
turn. Eligibility is applied before scoring/top-k. Cold filtered ingestion still
embeds only eligible source passages. Derived evidence, source provenance,
temporal validity and context budgeting use the existing recall pipeline.

Publication uses a private temporary file, block reservation where supported,
and atomic replacement under one short POSIX directory lock. It cannot replace
a newer source generation with an older reader's generation. Inference and
bulk writes hold neither that publication lock nor a SQLite writer lock. Frozen
stores may read matching snapshots but never create them. Hosts without POSIX
locking, insufficient disk space or oversized caches keep bounded SQLite
streaming. Interrupted inference removes its temporary file; subsequent
publication cleans temporary files belonging to terminated processes.

Completed snapshots are capped at 16 files / 4 GiB per cache directory, with
oldest files pruned. There is one lock file per directory. Private snapshot and
lock creation uses mode 0600, and lock opens reject symlinks where supported.
Existing readers can finish against an unlinked old inode while a new snapshot
is published. Active builders/old readers can temporarily add disk usage beyond
the completed-file cap. A large source update rebuilds the snapshot and reuses
the bounded persistent content-hash vector cache instead of constructing a
corpus-sized dictionary of mapped row objects.

The 128 MiB cap covers retained heap vector matrices, not total RSS. Mapped
pages may become resident in the operating system's page cache and exceed that
figure; the OS can reclaim them under memory pressure. A snapshot adds another
copy of the compact vectors plus 48 bytes of metadata per passage to disk.
Exact search remains O(eligible passages × dimensions × query facets).

## Measurement

Raw measurements, pinned baseline revisions, environment and top-ten passage
lists are in `retrieval-scale-results.json`. Use the existing reproducible helper
against independent checkouts:

```bash
OPENBLAS_NUM_THREADS=1 python research/profile_dense_facets.py CHECKOUT \
  --passages 200000 --dimensions 768 --queries 8 --persistent --runs 5
```

The workload uses deterministic normalized sine passage vectors, random query
vectors with seed 2026, and the real SQLite embedding-cache implementation.
Local inference is replaced with synthetic encoding so disk/vector mechanics
can be measured independently. Preparation occurs before timing; timed calls
scan an already prepared corpus. Generation model inference, HTTP transport and
official answer-quality judges are not measured. No model API is used.

| Revision | Median vector-search latency | Top-ten lists |
|---|---:|---|
| Original `4b555b0` | 37,601.310 ms | All eight match |
| Batched `8846fea` | 3,104.567 ms | All eight match |
| Mapped candidate | 573.884 ms | All eight match |

The incremental gain against the latest baseline is 5.41×. The current-session
original comparison is 65.52×, but timing variability matters: the earlier
original profile in `batched-retrieval-results.json` measured 22,527.588 ms,
which implies a more conservative 39.25× comparison with this candidate.
That variation is not an extra architectural improvement. Each retained revision
was run sequentially in the shared workspace; unrelated work can affect latency.
Initial simultaneous preparation/profiles were discarded because of CPU and disk
contention. These measurements establish improvement on this workload, not a
universal 20× gain or superior answer quality versus Mem0/other systems.

## Algorithm research and verification

Mem0's paper separates retrieval and end-to-end latency; this profile measures
the former's vector-search component only. Mem0's vector-store implementations,
Graphiti's indexed dense arms/RRF and Hindsight's PostgreSQL HNSW migrations
illustrate the benefit of amortizing corpus index work. Here that idea is
implemented independently as an exact mapped snapshot, avoiding an ANN recall
tradeoff or new database dependency. CommonTrace's independent sparse arm is
preserved so sparse-only evidence can remain a candidate. Source provenance,
temporal graph discovery and historical belief handling are preserved.

Fourteen new disk-cache regression cases cover connection reuse, exact filters,
private file modes, frozen stores, source updates/deletion and recycled ids,
older snapshots, malformed/truncated/well-formed incomplete files, dimensions,
failed inference, allocation/capacity fallback, pruning, orphan cleanup and
symlink-lock fallback. The existing bounded-streaming test now explicitly
disables both cache tiers so it continues to verify its fallback contract.
Targeted checks passed 74 cases, with Ruff and whitespace validation passing.
The complete conversation suite also passes. Comparable official answer-quality
evaluations remain separate; these latency measurements cannot establish
competitor-wide benchmark superiority.
