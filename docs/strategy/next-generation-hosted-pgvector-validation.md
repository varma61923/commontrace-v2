# Hosted projection with actual PostgreSQL and pgvector

Date: 2026-10-12. These are real database and extension correctness checks,
using deterministic vectors. They do not measure embedding quality, production
HNSW recall or the million-fact latency gates.

## Disposable environment

The owned PostgreSQL 16.15 cluster listens only on `127.0.0.1:59733`; its data
directory is `/tmp/commontrace-stream-pg16-_tyzsrhi`. Validation used the fresh,
exclusive `pgvector_test` database owned by `stream_app`, a role without
superuser or BYPASSRLS privileges. No retained database, index or default server
was used. The Hub test fixture recreates/truncates tables, so its DSN must never
point at a retained database. Database ownership here permits fixture DDL; these
checks alone do not prove migration-installed row-level-security enforcement.

The cloud runtime and networking skill references were read first. The current
tool list exposes no `environment_status` tool, and the policy snapshot file is
absent. Network operations retained inherited proxy/CA configuration and TLS
verification. No credentials or environment dump was read or printed.

Official [pgvector installation instructions](https://github.com/pgvector/pgvector/tree/v0.8.2#installation)
and the [PostgreSQL release announcement](https://www.postgresql.org/about/news/pgvector-082-released-3245/)
support the selected release. The pinned source is **v0.8.2**, commit
`cab9da72c04353f143bb06b42ab70a403daac64a`, cloned under
`/tmp/commontrace-pgvector-v0.8.2-validation`. This is an explicit compatible
version selection, not a claim that v0.8.2 is the latest release.

PostgreSQL server headers were absent. Rather than install the development
package's LLVM dependency tree, matching Ubuntu packages were downloaded and
extracted under `/tmp/commontrace-pgvector-build-headers`:
`postgresql-server-dev-16` and `libpq-dev`, both
`16.15-0ubuntu0.24.04.1`. Build command:

```bash
make -j2 PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config with_llvm=no \
  PG_CPPFLAGS='-I/tmp/commontrace-pgvector-build-headers/extracted/usr/include/postgresql/16/server -I/tmp/commontrace-pgvector-build-headers/extracted/usr/include/postgresql'
```

Only three extension files were installed outside `/tmp`:

- `/usr/lib/postgresql/16/lib/vector.so`
- `/usr/share/postgresql/16/extension/vector.control`
- `/usr/share/postgresql/16/extension/vector--0.8.2.sql`

`CREATE EXTENSION vector` was run as the cluster administrator in the owned
`pgvector_test` database, and at the coordinator's request in its owned
`root_integration` database. Both reported extension version **0.8.2**. No
package was installed, no service was restarted and no default cluster was
created. The installed shared library SHA256 is
`aeebacdfbc2f7c898a91e34e3472efb57ca79f9f8b4e672b44e9857a2cf9a5f2`;
it is a local native build artifact, not a portable release checksum.

## Existing vector and snapshot contracts

```bash
COMMONTRACE_TEST_PGVECTOR_DSN=postgresql://stream_app@127.0.0.1:59733/pgvector_test \
  /tmp/commontrace-goal-env/bin/python -m pytest \
  tests/test_vector_store.py tests/test_postgres_vector_snapshots.py -q
```

Observed: **46 passed in 9.52 seconds**, with no PostgreSQL cases skipped.
The existing vector suite covers SQLite, PostgreSQL exact and PostgreSQL HNSW
parameters, scoped candidate filtering, atomic validation, concurrent writes,
durability and deletion. The snapshot suite exercises actual pgvector SQL,
fenced publication, pinned readers, generation isolation and hard erasure.

## Canonical change-stream worker with actual vector backends

A temporary copy of `hub/tests/test_memory_projection.py` was saved as
`/tmp/test_commontrace_hosted_pgvector_projection.py`. Only its main projection
fixture changed: it parameterized `False/True` and opened
`PostgresVectorIndex(..., approximate=request.param)` against the exclusive
database instead of the initial SQLite index. The production HostedProjection,
canonical PostgreSQL transactions and all test assertions stayed unchanged.
Tests which intentionally replace a lost backend with SQLite retained that
replacement, providing cross-backend reconstruction checks.

```bash
HUB_TEST_DATABASE_URL=postgresql+asyncpg://stream_app@127.0.0.1:59733/pgvector_test \
COMMONTRACE_TEST_PGVECTOR_DSN=postgresql://stream_app@127.0.0.1:59733/pgvector_test \
PYTHONPATH=/root/Test/commontrace-v2 \
  /tmp/commontrace-goal-env/bin/python -m pytest -p hub.tests.conftest \
  /tmp/test_commontrace_hosted_pgvector_projection.py -q
```

Observed: **33 passed in 34.52 seconds**. The tests establish bounded draining,
content-cache reuse, zero new embedding calls after unchanged restart,
publication-before-acknowledgement recovery, cancellation/fence refusal,
generation-pinned reads, delete serialization, inactive update removal,
provider-failure lease cleanup, historical-vector erasure and identifier reuse.
They use deterministic fake embeddings and do not establish semantic quality.

## Authenticated route with PostgreSQL snapshots

The final route suite runs against PostgreSQL 16.15 with the extension enabled
in the canonical test database. Its vector fixture exercises both the real
SQLite snapshot backend and `PostgresVectorIndex` with HNSW enabled:

```bash
HUB_TEST_DATABASE_URL=postgresql+asyncpg://stream_app@127.0.0.1:59733/root_integration \
  /tmp/commontrace-goal-env/bin/python -m pytest \
  hub/tests/test_memory_hybrid_route.py -q
```

Observed: **13 passed in 15.75 seconds**, no skips. In addition to the dense
route and tenant checks, the suite verifies dense-only AtomicFact retrieval,
stale and refuted evidence rejection, foreign-scope and unmapped-lesson
rejection, correction after an acknowledged generation, and hard erasure of a
dependent fact through an old pinned reader. Embeddings are deterministic, so
this is route and storage evidence, not a model-quality or latency result.

## Actual HNSW planner evidence

The retained temporary script `/tmp/commontrace_pgvector_hnsw_plan.py` published
4,096 deterministic eight-dimensional vectors into a fresh owner namespace,
searched the production snapshot backend, and explained the same parameterized
snapshot query. It used the production `force_custom_plan`, strict iterative
scan and `ef_search=100` settings. It did **not** disable sequential scans to
force an index. The resulting plan, retained at
`/tmp/commontrace-pgvector-hnsw-plan.json`, selected
`commontrace_vector_versions_hnsw_2c624232cdd22177` naturally. Search returned
ten hits and ranked the query vector's own record `r00000` first. The script
erased its owned vectors after recording the plan.

```bash
PYTHONPATH=/root/Test/commontrace-v2 \
  /tmp/commontrace-goal-env/bin/python /tmp/commontrace_pgvector_hnsw_plan.py
```

This proves the real HNSW index can service the scoped snapshot query. A
single synthetic self-match and plan inspection cannot establish retrieval
recall, throughput or latency at production scale.
