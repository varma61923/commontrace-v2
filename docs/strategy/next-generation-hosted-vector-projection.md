# Hosted vector projection continuation

The hosted record stream now has a bounded projection worker backed by the
existing `VectorIndex.snapshots` contract. `HostedProjection` validates its
authenticated tenant, namespace, complete model revision and dimension against
the server-owned embedder. `ProjectionIdentity.key` hashes that identity; a
different model revision receives a separate watermark and embedding cache.

The worker holds the same tenant advisory transaction lock as canonical writes
while reading an ordered bounded batch, resolving cache misses and publishing.
This deliberately trades write concurrency within a tenant for a simple first
correct implementation: an edit or destructive erasure cannot commit while a
worker is embedding the old payload. Other tenants remain independent. The
maximum build/read lease is 300 seconds. Slow provider calls can hold the
canonical lock; this change does not establish the server write-latency gate.

Each publication uses a deterministic generation derived from the stable
canonical source identity, projection identity and target event sequence.
Fenced publication completes before the SQL watermark advances. If publication
succeeds and SQL acknowledgement rolls back, the next drain validates and
acknowledges the external head without embedding it again. If the external
index is absent or restored to an older valid head, replay resumes from its
actual revision. Committed per-record content hashes reuse vectors for
unchanged text, process restart and index reconstruction. Erased event payloads
and cache rows stay erased during reconstruction.

Hard deletion also calls the existing snapshot backend's new additive
`erase(keys)` operation before publication/acknowledgement. This removes every
historical version and private stage, revokes pre-erasure builders, and makes
subsequent reads through old pins omit the deleted key. `erase_records(ids)`
performs this and legacy-vector erasure under the owner lock without invoking
an embedding model or advancing the cursor. Ordinary inactive updates remain
soft snapshot deletions. A deletion followed by legitimate identifier reuse in
one batch still erases the old intervals before publishing the new content.

Queries pin exactly the generation recorded in SQL and return that cursor with
the hits. They never use the generic index search's legacy-vector fallback.
The journal must hydrate only current canonical rows whose `change_seq` is at
most that cursor and recheck scope, lifecycle and source evidence. An absent
published generation, foreign source binding, stale foreign head or embedding
failure propagates to the journal's lexical fallback. Raw projection candidates
are an internal API and cannot independently establish record visibility.

This reuses CommonTrace's SQLite/PostgreSQL fenced MVCC snapshot implementation
and scoped source binding, rather than adding another lease system. The
publish-before-watermark failure rule also follows the inspected competitor
Cognee source at revision `0ec7a9fa6`,
`cognee/modules/session_distillation/distill.py:511–530`: publish surviving
work, then leave the watermark unchanged on failed calls. No competitor code
was copied. Graphify's retained code graph identified `PostgresSnapshots`,
`SnapshotBackend`, `session_scope`, and the existing local fact outbox as the
reusable mechanisms.

## Validation

Before implementation, the new contract test failed at collection because
`hub.memory_projection` did not exist. The continuation suite exercises real
PostgreSQL 16 canonical transactions with both the real SQLite vector snapshot
backend and PostgreSQL/pgvector 0.8.2 with HNSW enabled. The embedder is
deterministic and counts calls. The final combined suite passed **87 tests in
48.41 s, with no skips**. The command, run against dedicated disposable
databases, is:

```sh
HUB_TEST_DATABASE_URL=postgresql+asyncpg://stream_app@127.0.0.1:59733/projection_test \
COMMONTRACE_TEST_PGVECTOR_DSN=postgresql://stream_app@127.0.0.1:59733/pgvector_test \
  /tmp/commontrace-goal-env/bin/python -m pytest \
  hub/tests/test_memory_projection.py tests/test_sqlite_vector_snapshots.py \
  tests/test_postgres_vector_snapshots.py tests/test_vector_store.py -q
```

Coverage includes bounded replay, unchanged text and restart cache reuse,
index-loss rebuild, independent model revisions, external publication followed
by SQL rollback, cancellation and refused publication, exact old-generation
reads while the external head is ahead, same-owner deletion waiting on
embedding, quarantined updates, malformed vectors, query-failure reader
cleanup, foreign source/head rejection, erased-history reconstruction, erasure
failure without acknowledgement, eager erasure without embedding, and identity
reuse after deletion. A new snapshot privacy regression first reproduced a
missing `erase` API, then passed against SQLite after implementation; its
PostgreSQL equivalent passed in the final integration run. The initial
projection/snapshot/vector-store run passed 37 tests with 34 PostgreSQL-vector
tests skipped; installing the extension in an owned disposable instance allowed
those tests and the new parametrized PostgreSQL worker cases to run. Ruff,
bytecode compilation, focused
mypy (`--follow-imports=silent` on the worker and three snapshot modules), the
generated documentation check and `git diff --check` passed. Unrestricted mypy
also follows existing unrelated dependencies and reports 246 errors in 62 files;
that is not represented as a passing repository typing gate.
The integration cases validate bounded lifecycle/erasure correctness with
PostgreSQL vectors and HNSW. They do not establish model-backed retrieval
quality, approximate recall at scale, million-record latency or retained
snapshot garbage collection. Old generations and tombstones are
retained until a separately validated collection policy exists; explicitly
erased vectors are removed from every generation immediately.
