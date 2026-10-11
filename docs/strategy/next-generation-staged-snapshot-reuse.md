# Reuse a staged payload only while it still describes the fact

P0 is still open: immutable lexical reuse reaches 1,741 and 1,756 facts/s
on the growing diagnostic copy. The source-bound profile still shows three
snapshots per new fact and a second encoding of the already-staged payload.

Retain the detached payload with the existing transaction-local projection.
At flush, reuse its encoding only for an exact AtomicFact whose declared
fields still contain the identical immutable scalar objects, or flat built-in
lists/dicts containing those same scalar objects. Changed values, nested
containers, evidence dataclasses, custom scalar/container types and fact
subclasses use the existing recursive snapshot and encoding path. Identity
checks deliberately distinguish bool/int and positive/negative zero even
when Python equality says they are equal. Equivalent replacements may fall
back harmlessly.

The final MemoryRecord is still constructed and validated, events still commit
atomically with all projections, and transaction cleanup drops the retained
snapshot. No mutable payload is memoized across writes. Add tests before the
runtime change for one encoding of unchanged ordinary facts, post-stage scalar
and container edits, nested/subclass fallback, and equal-valued invalid scalar
types. Keep all throughput misses and gate thresholds unchanged.
