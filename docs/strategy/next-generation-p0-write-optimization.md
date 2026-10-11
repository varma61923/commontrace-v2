# P0 write optimization

The continuation audit missed the million-fact write gates. Keep P0 open and
measure the real hierarchical APIs with the original thresholds. Do not change
SQLite WAL, FULL synchronization, conflict handling, evidence or scope checks.

Profile before changing storage: the 10,000-fact workload spends time opening
SQLite connections, projecting posting terms one SQL call at a time, and locking
the entity file even when extraction finds no entities. First skip entity-index
I/O for empty extraction results and batch the existing projection statements
with bound parameters. Projection updates must remain visible to dedup inside
the transaction and roll back together with their event ledger.

Add contract tests before implementation: entity-free writes must leave the
index untouched without accessing it; failed projection batches must leave no
postings or ledger events; repeated terms, updates, deletes and rebuilds must
retain consistent document counts. Reuse existing legacy/SQLite search, scope,
contradiction, crash and concurrency parity tests.

Use small-store profiles to choose further work, never as acceptance evidence.
If connection reuse is needed, retain only idle connections, bound retention,
keep each operation in its own transaction, and cover replacement, rollback,
nested readers, threads and forks before changing ownership. Fresh connections
remain necessary across process boundaries. Any remaining miss stops phase
advancement; hybrid/server/restart gates remain separate.

The full baseline test suite runs in the original worktree. Candidate changes
and their checks run in a separate worktree so measurements and validation can
identify the source that actually ran.

The implementation retains at most four idle connections across roots. Checkout
uses the canonical database path and file identity; active operations never
share a handle. Successful operations return their handle, failures discard it,
and every checkout gets a fresh transaction-local fact map. Drain idle handles
before forks, during process shutdown and before hard erasure. Call
`fact_store.close_connections(root)` after quiescing callers before moving,
replacing or deleting a store. Temporary benchmark stores drain before cleanup.
Forking during an active SQLite operation remains unsupported.

The batch conflict pass now maintains its later-id set incrementally, avoiding
quadratic copying while preserving comparison order on both storage backends.
Event serialization reuses the detached fact payload and serializes the validated
envelope directly; the public envelope and fact wire formats are unchanged.

SQLite documents transaction snapshot boundaries and the prohibition on using
or closing inherited SQLite handles in children:
[isolation](https://www.sqlite.org/isolation.html),
[fork and file ownership](https://www.sqlite.org/howtocorrupt.html#_carrying_an_open_database_connection_across_a_fork_),
and [WAL checkpoints](https://www.sqlite.org/wal.html).

Returning a handle also finalizes all live cursors. A partially consumed SELECT
can hold a WAL read mark after COMMIT; releasing a transaction therefore closes
its cursors before commit and reuse. Regression tests cover both connection-made
and explicitly created cursors, including a separate writer's TRUNCATE checkpoint.

Follow-up investigation reproduced five hook failures in isolation: the runtime
journal opened four connections before relevance selection, taking about 40 ms
in a diagnostic profile and exhausting the unchanged 50 ms deadline. Share the
bounded connection owner between the fact ledger and runtime journal, with a
separate four-handle bound for each store. Preserve each journal method's existing
transaction boundary and default FULL durability. Add tests first for nested
ownership, rollback/discard and partially consumed cursor cleanup. Keep the
million-fact measurements below tied to `54681d0`; follow-up code is unmeasured
at that scale until a new workload completes.

## Measured result: batch gate still missed

The corrected run used the real hierarchical APIs, default dedup/conflict policy,
SQLite WAL and FULL synchronization at one million synthetic facts:

| Metric | Before | After | Required | Decision |
| --- | --- | --- | --- | --- |
| Single-write p95 | 31.43 ms | 3.23 ms | <20 ms | Pass |
| One 1,000-fact batch | 759 facts/s | 1,181 facts/s | ≥2,000 facts/s | Miss |
| Sparse-overlap search p95 | 27.68 ms | 0.261 ms | No sparse-only target | Diagnostic only |
| Hybrid/server latency and zero-reembedding restart | Not exercised | Not exercised | Required | Unverified |

Phase advancement stops. The reproducer exited 1 on the measured batch miss.
The full next-generation objective remains incomplete. These are separate runs
on the same host with concurrent validation, not a controlled causal comparison.
The interrupted preliminary candidate is excluded from acceptance evidence.
No reader, judge or coding model ran. Reproduce with:

```sh
python -m benchmarks.next_generation_storage --sizes 1000,10000,30000,1000000 \
  --trials 20 --legacy-max 1000 --output /path/to/storage.json
```

The [raw storage report](measurements/p0-write-optimization-storage.json) binds
the measured source hashes. The run started from `a8d223d` with the candidate
changes present; those source files were subsequently committed as `54681d0`.

The [validation report](measurements/p0-write-optimization-validation.json)
records 92 passing storage, cursor, concurrency, fork, migration and erasure
contracts on the measured `54681d0` source. Ruff, both mandatory strict-mypy lists, Bandit's
configured gate and docs/OpenAPI drift pass. The pinned 154-question LoCoMo
[retrieval regression](measurements/p0-write-optimization-quality.json) passes
at 1,500 and 4,000 tokens; this is not answer accuracy. The current candidate's
full application suite completed with 6 failures, 6,666 passes and 118 skips;
no all-CI pass is claimed. The superseded baseline was intentionally interrupted. The unchanged
Hub completed 2,510 tests with 2 skips on dedicated PostgreSQL 16.

Postmeasurement fixes finalize cursors on aborted connection close and share the
bounded owner with the runtime journal. A root-autodetection test now imports
after clearing overrides in a fresh process. All 135 focused checks pass on the
follow-up, including the five hook failures reproduced on `54681d0`; production
deadlines remain unchanged. The native transport workload delivered all 50
directives but measured 152.27 ms p95, missing its <50 ms gate. These checks do
not remeasure the million-fact workload or constitute a completed full suite.
The new pool also passes strict mypy and is included in the mandatory CI list.

Follow-up Graphify extraction records 21,411 nodes, 63,537 edges, 297 modules,
303 reachability candidates and no dangling endpoints. Existing limits pass.

[Graphify](measurements/p0-write-optimization-graphify.json) records 21,399 nodes,
zero dangling endpoints, 303 static reachability candidates and 296 modules,
using the CI-pinned version and parser extras. No limit was raised.

The [signed manifest](measurements/p0-write-optimization-signed.json) binds these
four aggregate reports. Its signature detects edits and identifies the session
publisher; it does not establish scientific validity. Verify with:

```sh
python -m benchmarks.signed_results \
  --manifest docs/strategy/measurements/p0-write-optimization-signed.json --verify \
  --trusted-key "$(cat docs/strategy/measurements/p0-write-optimization-public-key.txt)" \
  --artifact-root .
```
