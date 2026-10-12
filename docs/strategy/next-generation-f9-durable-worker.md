# F9: durable worker and dead-letter state

The next-generation prompt lists a durable worker with a dead-letter state as
an F9 gap and points to EverOS OME triggers. The current CommonTrace branch
already has this capability in `commontrace/jobs.py`; a second queue or worker
module would duplicate its persistence and retry lifecycle.

CommonTrace stores jobs in `memory/jobs.db` through the shared SQLite/WAL
connection helper. Jobs are claimed transactionally with leases, and expired
leases are reclaimable after a process crash. Failures use capped exponential
backoff; after the configured attempt limit the job is parked as `dead`. The
CLI exposes list/show, run/watch, explicit retry, and purge operations. The
watch loop drains the active job on SIGTERM/SIGINT, while lease expiry covers a
hard process stop.

The queue has concrete product callers: `commontrace ingest --background`
queues document ingestion, hook lifecycle events enqueue session learning,
and wiki refresh and mental-model refresh enqueue their own work. The existing
`tests/test_jobs.py` exercises deduplication, retries, dead-letter/redrive,
backoff, lease reclaim, CLI operation, and the daemon hook. On this checkout,
`.venv/bin/pytest -q tests/test_jobs.py` passed 11 tests.

Graphify was used on the CommonTrace code graph to trace `jobs.enqueue()` and
`run_pending()` and on the EverOS graph to locate OME dispatch and retry paths.
The checked-in sources and tests are the authoritative evidence because the
generated graphs can lag working-tree edits.

EverOS's OME is a useful design comparison rather than a worker to transplant.
Its `triggers.py` defines Immediate/Cron/Idle dispatch descriptors; durable run
history and `DEAD_LETTER` state live in `RunRecordStore` and the runner. The
runner retries with capped jittered backoff and supports a dead-letter
callback. Its startup crash recovery explicitly documents that marking a run
crashed and adding a replacement scheduler job are not atomic. CommonTrace's
queue already provides a generic durable-work lifecycle and has direct callers,
so importing the OME scheduler would add a second dispatch model without
closing a demonstrated gap.

This satisfies the prompt's durable-worker/dead-letter item for the current
local product paths; it does not claim an externally deployed worker service.
Queue enqueue and the originating state change are not generally one atomic
transaction, so callers that require an outbox guarantee must define that
cross-store lifecycle explicitly before adding such a contract. The remaining
F9 work is tracked separately, including live/provider cost reconciliation and
per-query retrieval-quality telemetry.
