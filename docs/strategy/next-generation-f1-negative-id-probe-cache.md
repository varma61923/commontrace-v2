# F1: negative fact-ID probe cache candidate (rejected)

A transaction-local optimization candidate targeted a duplicate indexed read in
new fact insertion. `hierarchical.py`'s `_free_id()` checks `FactMap.__contains__()`
for a missing ID; the existing `__setitem__()` then reads `facts.payload` again
to learn the prior value is absent. The candidate cached that negative lookup
in the transaction's `before` map. Its focused SQL regression passed, and the
existing fact-store, conflict, deduplication and feature-cache tests passed.

The exact 1M workload did not validate the change. With the candidate installed,
single-write p95 was 7.216 ms and the 1,000-fact batch reached 1,843.770
facts/s, below the unchanged 2,000 facts/s threshold. A clean run on the
candidate-free storage path measured 6.198 ms single-write p95 and 1,858.199
facts/s, also below the batch threshold. Both runs used 20 trials and the same
benchmark command apart from the output path. They are separate runs, so their
0.8% batch-rate difference does not establish a causal regression or gain.
An earlier 1M reference measured 1,962.476 facts/s with a 507.78 second seed;
the clean baseline and candidate seeds were 521.95 and 520.19 seconds. The
candidate was removed because it did not pass the gate. Its exact-run report is
preserved at [the candidate measurement](measurements/p0-negative-id-cache-candidate.json)
and the clean run at [the baseline measurement](measurements/p0-negative-id-cache-baseline.json),
each with a SHA-256 sidecar. Their source-file hashes identify the measured revisions.

The Hindsight Graphify graph (`/root/Test/hindsight/graphify-out/graph.json`)
identifies `insert_facts_batch()` as its retain write path. Its Oracle batch
tests (`hindsight-api-slim/tests/test_db_abstraction.py:1117`) protect one
`executemany` call and reject row-by-row `fetchval` calls. That supports
profiling per-item database work as a target, but this SQLite-specific negative
lookup cache did not clear the CommonTrace throughput gate. The next candidate
must be measured with the same exact 1M workload before acceptance.
