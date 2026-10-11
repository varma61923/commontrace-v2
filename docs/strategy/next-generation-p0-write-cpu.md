# P0 write CPU simplification measurements

The production simplification is at `7e0a22402b9e69653c2fa190a374b42b92ccb83b`.
It removes the short-fact content-set cache after its binding-only experiment
showed no reliable gain, reuses one immutable configured JSON encoder, counts
the same conservative nested cache weight with fewer recursive calls, and
captures SQLite's INSERT row ID without a redundant SELECT. The design and
compatibility contracts are in
[write CPU simplification](next-generation-write-cpu-simplification.md).

The fresh exact-1M run used the unchanged `benchmarks.next_generation_storage`
workload on Python 3.12.3 / SQLite 3.45.1: 20 single-write samples followed by
one 1,000-fact batch on a freshly seeded million-fact store. Single-write p95
was **4.69 ms**, within the unchanged `<20 ms` target. The batch reached
**1,739 facts/s**, below the unchanged `>=2,000` target, and the benchmark
exited 1. Search measurements are sparse lexical timings only. The run does not
measure hybrid retrieval, PostgreSQL, restart embedding reuse, model answer
quality or causal learning lift.

The sealed earlier run on `aeadf5d` measured 3.86 ms and 1,167 facts/s. These
are two separate runs with concurrent full-suite validation; they do not
establish a causal performance improvement. The new run retained a separate
fresh million-fact fixture, leaving the earlier sealed fixture untouched.

On a growing mutable copy, the four frozen production rounds were 1,777,
1,821, 1,767 and 1,355 facts/s (baseline, candidate, candidate, baseline).
All miss 2,000; candidate timing is mixed with the running full-suite load and
is not an acceptance result. The eight binding-only helper rounds also all
missed 2,000. Every round, profile, source hash, imported module path and
canonical near-set/reader check is retained in the diagnostics artifact.

The 227 write/cache/evidence contracts, pinned 154-question retrieval-only
regression and source checks pass. The pinned quality regression reports no
change at 1,500 and 4,000 tokens; it does not run an answer reader or judge.
Graphify reports 298 Python modules and 300 reachability candidates, with no
dangling graph endpoints or policy violations. The frozen `cd8c1dc` full suite
completed with one failure and 6,748 passes. It exposed an event-version bug
when a mutation renames a fact; the descendant fix and focused regression are
recorded in [fact identity mutation evidence](next-generation-fact-identity-mutation.md).
The corrected source still needs a full suite run.

All raw results, test-first logs, source-bound commands and the signed public
aggregate are in
[the diagnostics](measurements/p0-write-cpu-diagnostics.json),
[the validation report](measurements/p0-write-cpu-validation.json),
[the fresh release result](measurements/p0-write-cpu-release.json), and
[the signed manifest](measurements/p0-write-cpu-signed.json). The signature
protects artifact integrity; it does not validate benchmark methodology.
