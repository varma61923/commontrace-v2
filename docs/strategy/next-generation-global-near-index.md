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

The production conversion on frozen `19c914b` preserves checksums of all event
rows and fact payloads on the private 1,044,020-fact copy. It takes 55.65 seconds
and reduces lexicon rows from 9,396,194 to 3,132,080; postings remain 24,012,460.
Two real-API batch diagnostics before conversion reach 1,028 and 1,172 facts/s;
after conversion they reach 1,101 and 1,178. These sequential warmed-copy samples
have concurrent validation load and do not establish a controlled throughput
improvement or the release gate. Adaptive candidate joins probe the smaller of
the block and term populations; the probe limit chooses a plan and never caps
matches. Nine new contracts pass, including strongest matches beyond 100 rows.
Frozen `d50d54b` also passes the pinned 154-question LoCoMo retrieval regression
at 1,500 and 4,000 tokens (paired comparison seed 61923). This uses no reader,
judge or embedding model and does not establish the answer-quality gates.

The first private combined/global and tighter-cache experiments had a quoting
mismatch in source replacement: bulk writes retained block-specific near terms
after conversion. Their document-frequency checks passed but did not establish
representation completeness. Preserve those raw results as **excluded mixed
layout diagnostics**. Repair only the mutable copy, and require every new fact's
global near-posting set to equal its canonical content, successful near recall
and no old near rows. The corrected prototype reaches 1,870–1,895 facts/s. An
additional FIFO/encoder experiment retains all samples, including 1,896 and
2,130 for its combined variant. Neither is production or release acceptance.

The next staging design must preserve FTS ranking during a batch: publish fact
metadata and FTS inside the owned transaction immediately, defer only lexical
postings/frequency maintenance, and merge a batch-local near-posting overlay.
Exact and contradiction lookups then keep the existing indexed SQL semantics;
semantic candidate ranking sees the same staged FTS rows. Finalize unchanged
staged postings together, refresh changed projections, validate the existing
record envelope and append all events atomically. Test semantic admission beyond
the pending-candidate cap, reinforcement, conflicts, evidence, removes/re-adds
and rollback before promoting this design. The current bulk prototype's full
projection deferral has not established those contracts.

Raw diagnostics, source recipes and validation are bound by the
[signed global near-index evidence](measurements/p0-global-near-signed.json).
