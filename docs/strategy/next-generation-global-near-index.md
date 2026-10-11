# Global near-duplicate terms with indexed numeric blocks

P0 remains open: latest release writes reach 1,167 facts/s at 1M, below 2,000.
The single-write target passes; full hybrid and restart gates remain unverified.
This design explores the persistent near-duplicate projection. No proposed
representation or combined prototype is accepted as a release gate.

The current lexicon includes the complete numbers/negations block in each near
term's kind. Independent numbered facts therefore insert the same common words
under distinct kinds. The retained million-fact workload makes this duplication
large. A private combined staged/bulk/event/cache prototype reaches about
1,470–1,500 facts/s, still below target; splitting those optimizations is not
sufficient.

Store near terms once under a distinct global projection kind and retain each
fact's numbers/negations block in its existing `facts.block` column. Add an index
on `(block,status)`. First test whether the query's block contains an active
fact. If it does, select the same required number of query terms and join their
postings through the indexed block before validating scope, status, forgotten
state and the original Jaccard predicate. Use global document frequencies to
order terms; this changes an optimization choice, not the admissible matches.

The existing Jaccard candidate argument permits any
`floor((1-threshold)*query_content_words)+1` distinct query terms: a qualifying
match cannot omit all of them. Keep the exact verification and deterministic
confirmation/id ordering. Preserve exact lookup, contradiction slots, numeric
ordering, negation blocking, FTS, BM25/overlap scores and all canonical admission.
Do not replace candidate filtering with a capped scan of the numeric block.

Upgrade derived postings atomically under the existing SQLite writer ownership.
Use a descriptive projection marker, recheck it after acquiring the writer
lock, and preserve authoritative event bytes and fact payloads. Existing readers
must retain a coherent old snapshot. A failed upgrade must leave a readable old
projection, and a later attempt must recover without a partially published
marker. Rebuild from the event ledger must reproduce the new projection.

Before production changes, add contracts for legacy conversion and checksums,
near recall/parity across blocks/scopes, existing-reader and concurrent-opener
behavior, injected migration failure, updates/removals/document counts, and
rebuild. If staging is needed, also test same-batch semantic candidates,
reinforcement, supersession, evidence validation and rollback. Any feature cache
must be bounded, contain immutable derived values, and physically clear at the
end of its owner operation; TTL alone does not release references.

Private diagnostics use separate copies of the sealed retained million-store.
They keep full WAL/synchronous durability, all API admission, all samples and
concurrent validation context visible. Only a later frozen-source real-API
million-fact reproducer can establish release throughput. Preserve the 2,000
facts/s gate and report a miss without phase advancement.
