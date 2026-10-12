# Hosted ordered projection stream

## Design and source evidence

The hosted record journal previously retained revisions keyed by record identity
and version, but did not expose a tenant-wide ordered change sequence. A durable
worker could neither resume incremental projection reliably nor distinguish an
index from another canonical store with the same record names.

Reuse the existing local outbox and vector snapshot engines. The local
`commontrace/fact_vector_projection.py::drain` consumes canonical sequence numbers
and publishes before acknowledging. Hindsight's
`hindsight-api-slim/hindsight_api/webhooks/manager.py:158`,
`WebhookManager.fire_event_with_conn`, queues work on the caller's transaction.
That checkout is MIT, copyright Vectorize AI, Inc., at
`a9eeed77f9bce99d8f97d39a30d3281747300824` (the original build prompt cited an
earlier revision). This is an adaptation of the transactional pattern, without
copying a new queue/backend or Hindsight's exception-swallowing behavior. Failed
enqueue must roll back the canonical mutation.

Graphify's existing CommonTrace graph at
`/tmp/commontrace-nextgen-final-20261012b/graphify-out/graph.json` was queried with
`graphify query 'MemoryRecordProjection ordered vector outbox transaction'
--graph ...`; source inspection confirmed the journal, local outbox, and existing
Postgres vector engine boundaries. No retained graph or index was regenerated.

## Storage contract

- `memory_change_clocks`: one atomic, rollback-safe sequence and stable UUID
  source identity per tenant. Concurrent appenders serialize through the clock
  UPSERT; canonical writers also hold the existing tenant advisory lock.
- `memory_change_events`: `(org_id, sequence)` ordered immutable record snapshots,
  or explicit deletion tombstones with SQL NULL payloads. Record-version
  history remains in `memory_record_events`; this stream is the projection
  outbox. The `(org_id, record_id)` index supports destructive redaction.
- `memory_records.change_seq`: ties each current record to its stream revision.
- `memory_projection_states`: an independent cursor and external generation for
  each full namespace/model-and-revision/dimension identity. Another model must
  not advance the first model's acknowledgement.
- `memory_embedding_cache`: vectors keyed by tenant, full projection identity,
  record identity, and content checksum. Restart/replay can reuse embeddings;
  record ownership in the key makes erasure deterministic across projections.
  Postgres rejects empty arrays and nonnumeric elements, including strings such
  as `"NaN"`. JSONB rejects actual nonfinite JSON numbers; the worker validates
  its provider output before insertion.

`append_change(session, org_id, record)` allocates and appends in the caller's
transaction; tombstones pass `record=None, record_id=..., version=...`.
`redact_changes(session, org_id, identities)` removes every historical snapshot
payload and every cached embedding for those identities, without touching
another tenant. Redaction and tombstone allocation roll back together if the
canonical erasure fails. After commit, the hosted route erases every registered
external snapshot identity under the same tenant lock. If that cleanup is
unavailable, the SQL tombstones remain durable; the projection worker erases
historical vectors again before acknowledging the pending deletion events.
Deleting SQL cache rows alone does not establish external vector erasure.

Retain ordered tombstones indefinitely in this implementation. No age-only
garbage collection can discard removals before a lagging or newly configured
projection observes them.

## Migration and tenancy

Migration `f8a0b3c4d5e6` follows `e7f9a2b3c4d5`, freezes its SQL definitions,
and locks canonical writes while building a deterministic current-state baseline
ordered by record ID/version for each tenant. It assigns `change_seq` and the
clock position consistently. It does not invent historical ordering from the
old per-record revision table.

The migration scopes each tenant separately and restores the previous
`app.org_id` setting. Existing forced RLS remains enabled throughout backfill,
including when migrations run as a non-BYPASSRLS schema owner. All four new
tables enable and force a fail-closed `org_isolation` policy for reads and
writes. Missing or empty `app.org_id` grants no rows. Downgrade removes the
derived stream/cache tables and sequence column, preserving canonical records
and their existing revision history.

## Tests-first evidence, 2026-10-12

Initial execution failed at import because no hosted change-stream API existed.
The first implementation then failed the erasure regression: SQLAlchemy's
default JSONB binding stored Python `None` as JSON `null`, violating the SQL NULL
tombstone constraint. `JSONB(none_as_null=True)` corrected both append and
redaction. Four additional tests demonstrated that array-only cache validation
accepted empty arrays and nonnumeric values; numeric/nonempty constraints now
reject those cases.

The final focused command ran against a newly initialized disposable PostgreSQL
16 cluster, owned test database `stream_test`, with `stream_app` a
NOSUPERUSER/NOBYPASSRLS role and an independently connected `stream_reader` role:

```sh
HUB_TEST_DATABASE_URL=postgresql+asyncpg://stream_app@127.0.0.1:59733/stream_test \
  /tmp/commontrace-goal-env/bin/python -m pytest hub/tests/test_memory_vector_events.py -q
```

Result: **10 passed in 9.52 seconds**, no skips. This covers allocation and
rollback, 24 concurrent appends across two independent tenant streams,
historical snapshot/cache erasure, erasure rollback, malformed requests,
invalid cached vectors, actual frozen migration/backfill/downgrade, forced RLS,
own/cross-tenant reads on all four populated tables, and denied unscoped and
cross-tenant writes through a non-owner reader. The dedicated migration test
requires a precreated `stream_reader` role; generic test clusters without it
skip that test explicitly.

The complete Alembic chain also upgraded a separate empty disposable database
as the same non-bypass schema owner, through `f8a0b3c4d5e6`, with exit code 0:

```sh
HUB_DATABASE_URL=postgresql+asyncpg://stream_app@127.0.0.1:59733/stream_migrations \
  /tmp/commontrace-goal-env/bin/alembic -c hub/alembic.ini upgrade head
```

```sh
./.venv/bin/ruff check hub/models.py hub/memory_vector_events.py \
  hub/alembic/versions/f8a0b3c4d5e6_memory_vector_stream.py \
  hub/tests/test_memory_vector_events.py
```

Ruff passed. These are correctness measurements on a small disposable fixture,
not the million-record write/ingest/search latency gates or a model-backed
quality result. Zero re-embedding on worker restart requires the worker's
separate replay tests; this schema enables that behavior but does not prove it
by itself.
