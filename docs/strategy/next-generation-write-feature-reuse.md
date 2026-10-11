# Transaction-owned lexical feature reuse

P0 remains open. The FTS-preserving staged write path on `ce1abe2` reaches
1,601 and 1,645 facts/s on the growing private million-copy, below 2,000.
The source-bound profile shows repeated normalization, signatures and stemming
across deduplication, projection and conflict detection. These diagnostics run
with concurrent validation and do not establish a release throughput gate.

Reuse only pure lexical values: normalized strings, signatures, content-word
sets and contradiction slots. A write transaction owns its cache through a
ContextVar. Nested writers get separate owners and restore the parent; threads
and tasks keep their own contexts. Reads create no additional owner. Scope,
status, evidence, timestamps, mutable facts and model decisions are never cached.

Bound each owner to 4,096 entries and 8 MiB of accounted retained weight. Count
immutable Python values and their children with a conservative per-entry
allowance; this is not a process-RSS limit. Clear the cache when admission would
exceed either bound, compute oversized values without retaining them, and reject
mutable/custom values from cache admission. Always clear entries and accounting
at owner end, including aborted transactions, then restore the context token.
TTL alone would not release references.

Test lexical parity, identity reuse, immutable values, small-budget eviction,
oversized/mutable fallback, commit/abort cleanup, nested writers and thread
isolation before implementation. Keep the same fact/record validation, postings,
FTS ranking, durability, deadlines and acceptance thresholds. Measure against
frozen `ce1abe2`, retain all samples, then use the unchanged fresh exact-1M
real-API reproducer for release acceptance.
