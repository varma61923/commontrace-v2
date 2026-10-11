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
