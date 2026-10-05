# Incremental ingestion and retention

Baseline: `8846feaccf94f6ad9dc37787c3c1f8fb63acdcca`.
The implementation independently applies incremental maintenance and source-scoped
belief revision. Mem0's update operations and Graphiti's historical validity
model motivate preserving changed beliefs without rewriting unrelated histories.
Honcho's bounded scope backfill and Hindsight's retention batching motivate
processing new source batches instead of rereading old transcripts. No competitor
source was copied; these are CommonTrace SQLite operations.

## Changes

Deleting a session or purging expired evidence formerly reconstructed and updated
every slotted fact in the space. Maintenance now identifies the owner/slot chains
whose facts lose any supporting premise, repairs only those chains, and writes
only changed successor pointers. Facts anchored on a surviving message are still
removed when another required source is deleted, preserving CommonTrace's existing
conservative provenance contract. Old surviving beliefs are reinstated in their
original chronological order, including backdated arrivals. The repair and evidence
deletion share one transaction and also support nested transactions.

Extraction formerly materialized and decoded every message in a session before
filtering its extraction checkpoint. `session_turns` now accepts `after_idx`,
`through_idx`, and `limit`; extraction reads at most 40 new complete messages per
batch using the session/index key. A start-of-run upper checkpoint ensures
concurrent appends are handled on the next run. Already completed histories need
no message decoding. No database transaction remains open across a model call.
Each batch also retains source-content fingerprints. Before publishing memories
or an extraction checkpoint, `add_memories(expected_sources=...)` rechecks those
exact source rows under its write transaction. A deleted-and-recreated session
can recycle both a SQLite turn id and an external message reference; matching ids
alone cannot establish provenance. Changed content, speaker, role, date, index or
reference rejects the stale extraction with a retry error. Unrelated concurrent
appends remain valid because validation covers the read source batch.
This is a row bound, not a byte/token bound: an unusually large message can still
produce a large extraction prompt. Full-message citation semantics remain intact.

Deleting a whole session removes its extraction checkpoint. Purging the tail of
a surviving session rewinds the checkpoint to the surviving high-water mark,
because subsequent appends can reuse the removed turn indices. These changes fix
new observations being silently skipped after retention and session reuse.

`set_summary(..., expected_revision=...)` can reject publication when source units
changed during model generation. It compares the space revision while holding the
write transaction; this conservatively also rejects unrelated-session changes.
Existing callers retain their original behavior unless they supply a revision.

## Reproducible local measurements

Each measured operation uses a fresh connection and an identical copied SQLite
store. Store construction, connection opening, corpus setup and file copying are
outside the timings. Five operation timings are recorded. Extraction uses a
completion stub returning no memories, measuring preparation/checkpoint overhead,
not local-model generation or memory-extraction quality. Tracemalloc peak is
recorded in a separate sixth run to avoid instrumentation affecting latency.

| Workload | Baseline median | Improved median | Speedup |
|---|---:|---:|---:|
| Delete one session with 20,002 facts across 10,001 owner histories | 52.528 ms | 0.760 ms | 69.1× |
| Resume extraction after 20,000 old messages, with five new messages | 164.121 ms | 2.387 ms | 68.8× |

Deletion writes fell from 20,021 to 21 SQLite changed rows; the surviving target
belief was identical in all five trials. Extraction decoded ten source rows instead
of 20,005: five new messages and five provenance rechecks, while producing the
same call count and final checkpoint. Peak traced
Python allocation fell from 18,837,572 bytes to 16,739 bytes. These gains are
specific to incremental workloads, not a promise of a universal 20× acceleration.
They do not establish performance versus Mem0 or official benchmark accuracy.

Raw samples, environment, revision and workload parameters are in
[ingestion-performance-results.json](ingestion-performance-results.json).
Use the same scripts for both checkouts:

```bash
python research/profile_deletion.py /path/to/baseline --owners 10000 --runs 5
python research/profile_deletion.py /path/to/candidate --owners 10000 --runs 5
python research/profile_extraction.py /path/to/baseline --turns 20000 --pending 5 --runs 5
python research/profile_extraction.py /path/to/candidate --turns 20000 --pending 5 --runs 5
```

Regression coverage includes untouched owner histories, backdated corrections,
non-anchor evidence loss, source filters, nested rollback, deleted-session reuse,
tail checkpoint reuse, bounded new-message batches, concurrent append deferral,
completed-session reads, and recycled source ids/references with changed content. The existing archive/provenance/temporal tests remain
applicable. Repair remains proportional to the affected histories: deleting a
large fraction of a single long belief chain still requires chronological repair.
