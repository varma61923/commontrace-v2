# F3 Lance handle reuse investigation

## Decision

Do not add a process-local Lance connection/table cache to the fact projection
yet. Reusing a table handle with LanceDB's default consistency setting can miss
vectors published by another process. Enabling strict cross-process freshness
checks repairs that behavior, but makes every read perform the check. More
critically, forked-child behavior was inconsistent in the installed LanceDB
runtime: one probe crashed, a production-shaped probe hung, and a minimal probe
succeeded. The library itself warns that fork support is experimental. This is
not enough evidence to claim a bounded cache is safe across forks.

The result is a no-go for this optimization under the current process and
freshness requirements, not a claim that LanceDB handles can never be reused.

## Current path and motivation

Graphify traces `fact_vector_projection.search()` through `_index()` to a new
`LanceVectorIndex` for each search. `LanceVectorIndex.__init__()` calls
`lancedb.connect()` and creates or opens the scoped table; `search()` executes
against that handle, and the projection closes its wrapper after the query.
`LanceVectorIndex._run()` serializes operations on one instance with a thread
lock. A cache could avoid repeated connection and table setup.

The measured 1M synthetic hybrid run reports dense-projection p95 of 223.07 ms
in [`f1-hybrid-1m-synthetic-lance.json`](measurements/f1-hybrid-1m-synthetic-lance.json).
The separate Lance component report measured 19.69 ms p95 with mean recall@10
of 0.995 in [`p0-lance-refined-million-scale.json`](measurements/p0-lance-refined-million-scale.json).
These figures make setup reuse worth investigating, but they do not establish
that setup is the full difference or that reuse closes the hybrid latency gate.

## Freshness reproduction

The experiment used the installed LanceDB 0.40.0 package in
`/tmp/commontrace-vector-env` with Python 3.12. A parent process created a
two-dimensional table and inserted `old`. A separate Python subprocess opened
the table and committed `new`, advancing the table from version 2 (one row) to
version 3 (two rows). The parent then searched using its already-open table
handle:

| Connection setting | Parent handle after subprocess write |
| --- | --- |
| Default, `read_consistency_interval=None` | Still version 2, one row; search returned only `old` |
| Strict, `read_consistency_interval=timedelta(0)` | Version 3, two rows; search returned `new` and `old` |

The [official Python API reference](https://lancedb.github.io/lancedb/python/python/)
documents this contract: `None` disables consistency checks for performance;
zero checks every read for updates from other processes; smaller intervals
increase read I/O and cost. A positive interval permits an eventual-consistency
window, which does not meet immediate publication visibility.

Thus a reused handle can preserve cross-process visibility only with strict
refresh in the tested API. A refresh interval above zero allows a stale-read
window, while leaving the default misses the newly published vector on that
retained handle until it is refreshed or reopened.

## Process, root, and concurrency checks

Fork behavior is not dependable enough to support a safety claim. With the same
installed 0.40.0 runtime, the exploratory raw LanceDB fork probe produced child
exit code `-11` in one run. A later raw, minimal fork probe succeeded. A
production-shaped probe first published and searched through
`LanceVectorIndex` (initializing its worker runtime), then forked a child that
created a new `LanceVectorIndex` and attempted an upsert. LanceDB emitted its
experimental-fork warning and the child did not finish; the test process was
stopped after 32 seconds. The warning says the internal async runtime is reset
in the child and a small deadlock risk remains if other state was active at
fork time; it recommends `forkserver` or `spawn`. The mixed outcomes mean a
cache-level PID check alone cannot prove safety.

A small two-root concurrency smoke test did pass: separate
`LanceVectorIndex` objects in separate temporary roots accepted concurrent
upserts and repeated alternating searches without cross-root result leakage.
This confirms the current separate-instance behavior only. It does not validate
an LRU cache, leases during eviction, or fork lifecycle. Any future cache must
key entries by resolved root and the complete vector scope/model/dimension, and
must keep an entry alive until all concurrent operations release it.

## Graphify competitor traces

Graphify was used on the current CommonTrace graph and the Hindsight, Graphiti,
and EverOS repository graphs:

- CommonTrace: `fact_vector_projection.search()` in
  `commontrace/fact_vector_projection.py:117` creates the Lance wrapper per
  call; `LanceVectorIndex.__init__()`, `.search()`, and `._run()` in
  `commontrace/vector_lance.py:25,122,54` are the relevant connection and
  operation nodes.
- EverOS: `src/everos/infra/persistence/lancedb/lancedb_manager.py:29,39`
  keeps process-wide connection/table objects behind an async lock and provides
  explicit disposal. `src/everos/config/settings.py:461` exposes the consistency
  interval, and `src/everos/core/persistence/lancedb/connection.py:48` converts
  it to LanceDB's `timedelta` parameter. This is the closest adaptation, and
  its explicit consistency control reflects the same freshness tradeoff
  measured here.
- Graphiti: `graphiti_core/graphiti.py:224,453` shows `Graphiti` owning a
  long-lived graph driver and exposing `.close()`. This supports
  lifecycle-scoped client reuse as a general pattern, but does not establish
  LanceDB's local-table consistency or fork guarantees.
- Hindsight: `hindsight-api-slim/hindsight_api/engine/db/base.py:206,341`
  defines an explicit database pool lifecycle and connection-acquisition
  interface. It is a conventional server-database pool, not evidence about
  reuse of an embedded LanceDB table handle.

## Revisit criteria

Reconsider reuse after the runtime contract is narrowed or strengthened: either
the application guarantees that Lance is first initialized after worker
forking (or mandates `spawn`/`forkserver`), and tests this lifecycle; or the
installed LanceDB version documents and passes post-fork read/write tests. Any
implementation must also retain immediate visibility after a second process
publishes vectors, bound cached entries across multiple roots, make concurrent
eviction safe, and measure the full 1M hybrid API with strict freshness enabled.
