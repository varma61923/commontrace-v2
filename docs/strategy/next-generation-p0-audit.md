# P0 continuation audit

This audit follows the attached next-generation build prompt on
`codex/next-generation-complete`. Existing P0–P4 implementation does not prove
the release gates. Start with current source, Graphify paths, foundation tests
and the real hierarchical write workload before proceeding to another phase.

The storage reproducer records its gates but currently exits successfully even
when a measured million-fact write target fails. Make a measured write miss
return nonzero so `reproduce.sh` stops. Keep smaller exploratory workloads usable
and preserve their explicitly unverified million-fact gates. Sparse timing does
not establish hybrid latency, and this workload cannot establish restart without
re-embedding. Test the strict 20 ms boundary and inclusive 2,000 facts/s boundary
before changing the reproducer.

Inspect the two CodeQL alerts identified by the prompt against current code and
authentication tests. Dismiss only those alerts with specific reasons, and
record the rationale beside the CodeQL configuration without suppressing query
families or source paths.

Use the CI-pinned Graphify version and parser extras before interpreting a
reachability-budget failure. Record missing parser coverage and tool-version
differences separately from application regressions.

## Measured result: stop at P0

The 2026-10-11 run used real hierarchical APIs and one million synthetic facts:

| Gate | Measured here | Required | Decision |
| --- | --- | --- | --- |
| Single-write p95 | 31.43 ms | <20 ms | Miss |
| One 1,000-fact batch | 759.25 facts/s | ≥2,000 facts/s | Miss |
| Sparse-overlap search p95 | 27.68 ms | No sparse-only acceptance target | Diagnostic only |
| Hybrid/server latency; restart without re-embedding | Not exercised | Required by the brief | Unverified |

Phase advancement stops; the full next-generation objective remains incomplete.
Seeding ran alongside validation on this host. The older 5.031 ms / 3,052 facts/s
release report is retained; this is not a controlled comparison with its hardware.
No reader, judge or coding model ran. Competitor benchmark claims are not results
measured here.

The [raw report](measurements/p0-continuation-storage.json) records the command,
source commit and source hashes. It ran the pre-fix benchmark and exited zero
despite the misses, independently demonstrating the exit-status defect. The
fixed exit behavior is checked with injected metrics: three failures and two
passes before the change, five passes after. Those fixtures do not establish
performance. Reproduce the actual workload with:

```sh
python -m benchmarks.next_generation_storage --sizes 1000,10000,30000,1000000 \
  --trials 20 --legacy-max 1000 --output /path/to/storage.json
```

The [validation report](measurements/p0-continuation-validation.json) records
93 foundation passes (23 skips), 119 authentication passes with Hub dependencies
(3 skips), and 29 explorer/storage-boundary passes. Ruff, the mandatory mypy lists,
Bandit's configured gate, dependency audits, docs/OpenAPI drift and the pinned
154-question LoCoMo retrieval regression gate pass. LoCoMo retrieval did not change
at either budget; this is not model-judged answer accuracy. The PostgreSQL Hub
suite completed with 2,510 passed and 2 skipped on a dedicated PostgreSQL 16
instance with default durability. The baseline application suite was superseded
and intentionally interrupted after the corrected candidate's full suite started;
its partial run is not a completed-suite pass. Current candidate validation is
recorded with the [write optimization](next-generation-p0-write-optimization.md).
No all-CI pass is claimed.

[Graphify evidence](measurements/p0-continuation-graphify.json) records the exact
tool and repository revisions: 21,370 CommonTrace nodes, zero dangling endpoints,
303 candidates and 296 modules. No reachability limit was raised. Competitor graphs
cover the listed mem0 and Graphiti subdirectories, not all eight prompt snapshots.
GitHub confirms the two named alerts are dismissed; PR #107 is already merged.

The [signed aggregate manifest](measurements/p0-continuation-signed.json) binds
all four reports. Its session key detects edits and identifies this publisher;
it does not certify the measurements. Verify against the repository-pinned key:

```sh
python -m benchmarks.signed_results \
  --manifest docs/strategy/measurements/p0-continuation-signed.json --verify \
  --trusted-key "$(cat docs/strategy/measurements/p0-continuation-public-key.txt)" \
  --artifact-root .
```
