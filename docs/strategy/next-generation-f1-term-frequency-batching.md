# F1 near-term frequency batch candidate

The SQLite near-duplicate lookup read each token's document frequency with a
separate indexed query. A test-first candidate loaded those counts with a bound
`IN` query, chunked at 200 terms to remain below older SQLite variable limits.
The focused trace test reproduced seven point queries for one seven-token
lookup; with the candidate installed, the fact-store and hierarchical suites
passed 73 tests.

Graphify exploration of Graphiti's bulk edge persistence and Hindsight's
`insert_facts_batch()` tests suggested checking for batchable database work.
Those implementations informed the experiment; no competitor code was copied.

The same-venv 30K diagnostic measured 2,163.626 facts/s before and 2,159.799
facts/s with the candidate, but it did not exercise the changed path. The
acceptance harness generates `item{index}` statements with a unique number in
each statement. `StatementIndex.near()` checks the `(numbers, negations)` block
first and returns before reading token frequencies when that block is absent.
The crafted seven-token regression did exercise the candidate; the benchmark
pair did not. I removed the candidate because it did not target the measured
workload, and the throughput numbers cannot be treated as evidence about its
performance. The full source hashes, command, limits and gate status remain in
the [baseline report](measurements/f1-term-frequency-batch-baseline-30k.json)
and [candidate report](measurements/f1-term-frequency-batch-candidate-30k.json),
each with a SHA-256 sidecar. A new profile was required to select an optimization
on the exercised path.

A later detached-snapshot candidate measured 2,071.287 facts/s in one exact 1M
batch and cleared the threshold narrowly; see the
[snapshot-reuse note](next-generation-f1-snapshot-reuse.md). This does not
change the conclusion that term-frequency batching itself was irrelevant to
the measured workload.
