# F3 hosted fact and vector event boundary

## Decision

The hosted hybrid gate remains open. This slice makes no Hub code or migration
change: the available Hub record model, event cursor, vector publication worker,
and query embedding path do not together satisfy the canonical `AtomicFact`
contract. Wiring the existing optional vector search into the REST route would
only turn on dense candidates; it would not make their freshness replayable.

## Hub evidence and blockers

`PostgresJournal.put` atomically appends a versioned `MemoryRecordEvent` and
upserts `MemoryRecordProjection` under the tenant transaction
([hub/memory_records.py](../../hub/memory_records.py#L34),
[hub/memory_records.py](../../hub/memory_records.py#L58)). This is useful
canonical-write infrastructure, but its event key is `(org_id, id, version)`;
there is no ordered sequence suitable for projection watermarks
([hub/models.py](../../hub/models.py#L806),
[d6e8a1b2c3f4](../../hub/alembic/versions/d6e8a1b2c3f4_memory_record_events.py#L19)).
The `MemoryRecord` envelope also does not define a lossless mapping for the
local `AtomicFact` fields and their evidence, revision, temporal, scope, and
lifecycle semantics ([commontrace/hierarchical.py](../../commontrace/hierarchical.py#L45)).
Treating rows with `kind="fact"` as canonical `AtomicFact`s would silently
change that contract.

Hub's optional dense arm accepts a vector and bounds candidate hydration by
tenant, scope, kind, status, and `is_latest` before visibility and derivation
checks ([hub/memory_records.py](../../hub/memory_records.py#L150)). The live
record route constructs `PostgresJournal` without a vector index and calls
search without a vector ([hub/memory_records.py](../../hub/memory_records.py#L534),
[hub/memory_records.py](../../hub/memory_records.py#L566)); it has no supported
server-side query-embedding contract. Writes enqueue no vector upserts.

Forgetting is stronger but deletion-only: when a vector backend is injected,
`forget` records tenant-scoped identities in `MemoryVectorErasure` in the same
transaction as canonical removal, and the bounded retry deletes remotely before
clearing those rows ([hub/memory_records.py](../../hub/memory_records.py#L396),
[hub/memory_records.py](../../hub/memory_records.py#L448)). This supports retry
for a single configured vector index, but it is not an ordered upsert/tombstone
stream, and its event history is removed with the canonical rows. The existing
RLS migration covers the three record and erasure tables; any new event and
watermark tables need their own fail-closed RLS policies and tests
([d6e8a1b2c3f4](../../hub/alembic/versions/d6e8a1b2c3f4_memory_record_events.py#L13)).

The local implementation provides the closest safe pattern to adapt, not a
server implementation: `fact_vector_projection.drain` reads ordered canonical
events, publishes idempotent upserts/deletes, then advances its watermark;
search filters stale dense IDs that have events beyond that watermark
([commontrace/fact_vector_projection.py](../../commontrace/fact_vector_projection.py#L58),
[commontrace/fact_vector_projection.py](../../commontrace/fact_vector_projection.py#L117)).
It is backed by the local SQLite fact ledger and cannot serve as Hub's
PostgreSQL event contract by itself.

## Competitor patterns worth carrying forward

- Hindsight queues webhook delivery rows using the same database connection as
  the primary transaction (`hindsight-api-slim/hindsight_api/webhooks/manager.py`,
  `fire_event_with_conn`; retain builds the callback in
  `hindsight-api-slim/hindsight_api/engine/memory_engine.py`,
  `_build_retain_outbox_callback`). This confirms the transactional-outbox
  boundary for durable work; the outbox is for webhook delivery, not vector
  freshness or identity-keyed model watermarks.
- Graphiti's Neo4j operations accept an explicit transaction for writes and
  bounded bulk save methods (`graphiti/graphiti_core/driver/operations/entity_node_ops.py`,
  `save_bulk`). That is a useful transaction-and-batching API shape, but its
  graph transaction does not coordinate CommonTrace's PostgreSQL facts with an
  external vector index.
- Cognee's `tasks/storage/add_data_points.py` writes graph artifacts before
  vectors and explains why vector-first partial success can leave unowned,
  retrievable points. It also documents a separate provenance-attachment
  window for hybrid backends. This supports sequencing recoverable side effects
  and making failure windows explicit; it does not supply a replay cursor.
- EverOS's Cascade quiesce route drains before stopping and returns pending and
  failed counts (`EverOS/src/everos/entrypoints/api/routes/cascade.py`). Its
  readiness reporting is a useful model for exposing backlog, but its file-scan
  queue is not a tenant-scoped canonical event ledger.

These examples inform mechanics only; none provides the missing hosted
`AtomicFact` source event and complete vector freshness contract.

## Required implementation slice

Before enabling hosted hybrid retrieval:

1. Choose and validate a canonical hosted representation for `AtomicFact`, with
   an explicit lossless conversion and identity/version rules. Do not infer it
   from `MemoryRecord.kind`.
2. In the same Postgres transaction as every canonical fact insert, revision,
   lifecycle change, or deletion, append a tenant-scoped monotonic event carrying
   an immutable fact snapshot or tombstone. Preserve tombstones until every
   configured projection has acknowledged them.
3. Add projection watermarks keyed by tenant and full vector identity
   (namespace, model/provider revision, and dimension). Drain bounded ordered
   batches idempotently; advance only after embedding plus vector upsert/delete
   succeeds. Fence competing workers and make restart replay safe.
4. Provide a server-owned query-embedding interface that selects the same full
   vector identity as the projection. Merge the bounded unprojected event tail
   or fence stale vector IDs, then hydrate from canonical rows and re-run tenant,
   scope, lifecycle, temporal, authority, provenance, and evidence checks before
   fusion or return.
5. Add migration and RLS tests plus restart/failure tests for interrupted vector
   publication, concurrent edits, deletion, projection-identity changes, and
   cross-tenant access. Then measure the hosted gate against that tested path.

Until this end-to-end slice exists, Hub remains lexical-only through its live
record route; no hosted freshness or hybrid-latency claim follows from the
optional vector branch exercised by injected unit tests.
