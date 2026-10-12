# Hosted record expiration and reaping

Hub records already accept the shared `payload.forget_after` ISO 8601 timestamp and require a nonempty
`payload.forget_reason`. `record_visibility.visible()` hides a record when the timestamp is due, so search,
profile, and runtime recall stop returning it immediately. Physical erasure is a separate bounded cleanup step.

`PostgresJournal.reap_due(limit=16)` selects at most 100 due projections for its authenticated organization,
in timestamp and identity order. It reads each record's stored reason and passes the selected projection version
to `forget`. `forget` rechecks that version under the tenant write lock before it erases the record and its
derived records. If a concurrent writer changed the selected revision, the reaper skips that candidate and
leaves it for a later pass. Existing vector-index erasure continues through the durable outbox. The response
contains the same per-record erasure receipts as explicit forget.

The Hub exposes this as `POST /v1/memory/record-reap-due` and the parallel `/api/v1/memory/records/record-reap-due`
route. It takes an optional `limit` from 1 through 100, defaults to 16, derives organization ownership from the
API key, and requires `SCOPE_ADMIN` because one invocation covers all user scopes in that organization. A tenant
scheduler can call this operation with a dedicated admin key; Hub does not start a global worker that would need
to enumerate organizations. No database migration is needed because expiration remains in the versioned JSON
record envelope. The due-record query is result-bounded, but it can scan the tenant's projection table, and the
existing cascade still reads that tenant's event history; add an expiry index or bounded cascade only after hosted
volume measurements justify those schema and semantic changes.

The local Journal is the semantic source: `commontrace/memory_journal.py::Journal.reap_due` uses the record's
stored reason and expected version, while `commontrace/record_visibility.py::visible` hides due records before
reaping. The hosted operation follows those rules and reuses Hub's existing derived-record/vector-erasure path.

Competitor comparison: Mem0's implementation stores an `expiration_date` and filters expired candidates from
search by default (`mem0/memory/main.py::_payload_is_expired`, `_get_all_from_vector_store`, and
`_search_vector_store`; tests in `tests/test_main.py::test_search_hides_expired_memories_by_default`). That
confirms the useful recall rule—expired memories are hidden before cleanup—but its date-only field and optional
`show_expired` behavior do not fit CommonTrace's timestamp, required-reason, and hard-erasure contract.

Sources were inspected at CommonTrace repository revision `91d0fb8e3f8ca64b5b388c10ad92fa0775a7429e` and Mem0
revision `b7ad69afda6b6ed030347c66d48a13e4de9dec08`; the CommonTrace working tree also contains concurrent
uncommitted changes from the larger feature effort.

Earlier validation in this workspace: the focused local record/OpenAPI checks passed (10 passed, 1 skipped), and the
combined local retrieval, journal, observation, and report slice passed 101 tests with 1 optional skip. Hub's
bounded-reaper and dense-batch unit contracts passed (2 passed); 48 database-backed Hub cases skipped because
no PostgreSQL instance was running. PostgreSQL dialect compilation of the expiry query passed, and gateway OpenAPI
generation checks and `git diff --check` passed. The TypeScript client includes a bounded `reapDueRecords()`
method and its SDK suite passes 38 tests.

## Disposable PostgreSQL validation

A later validation found installed PostgreSQL binaries and created a fresh
isolated cluster under `/tmp`, bound only to `127.0.0.1:55741`. The explicit
`HUB_TEST_DATABASE_URL` selected only its newly created database; the fixture's
schema drop/create and per-test truncation did not reach a retained database.
The server was PostgreSQL 16.15, and the database owner had neither superuser
nor RLS-bypass privileges. Both complete requested files passed:

```text
hub/tests/test_memory_records.py hub/tests/test_rest.py
50 passed in 28.55s
```

This executes expiration visibility before cleanup, bounded reaping,
stale-version rejection, derived-record cascades, durable vector-erasure
receipts/retries, and the REST admin/tenant boundaries against PostgreSQL.
The external vector engine remains a test double; these results do not verify
pgvector/HNSW behavior or hosted retrieval latency. The installed PostgreSQL
has no `vector` extension, but these Hub models require only built-in JSONB
and text-search types, so that does not block this slice.

The first run passed 49 tests and failed the existing RLS role-creation test
because the application role intentionally lacked `CREATEROLE`. Creating its
`commontrace_record_reader` role as the disposable administrator and granting
the application role permission to `SET ROLE` resolved the fixture setup.
No production or test code changed. The test then exercised unscoped denial
and tenant-scoped reads under that nonowner reader role. After validation,
`pg_ctl -m fast -w stop` stopped the owned server and `pg_ctl status` reported
`no server running`.

Reproduce from the repository root as an operator able to run commands under
the local `postgres` OS account. Every database operation below targets the
fresh cluster; the exit trap stops it:

```bash
set -eu
CT_HUB_PGBIN=/usr/lib/postgresql/16/bin
CT_HUB_PG_DIR=$(mktemp -d /tmp/commontrace-hub-disposable-pg-XXXXXX)
CT_HUB_PG_PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
chown postgres:postgres "$CT_HUB_PG_DIR"
runuser -u postgres -- "$CT_HUB_PGBIN/initdb" \
  -D "$CT_HUB_PG_DIR/data" --auth=trust --encoding=UTF8 --no-locale
runuser -u postgres -- "$CT_HUB_PGBIN/pg_ctl" \
  -D "$CT_HUB_PG_DIR/data" -l "$CT_HUB_PG_DIR/postgres.log" \
  -o "-h 127.0.0.1 -p $CT_HUB_PG_PORT -k $CT_HUB_PG_DIR -F" -w start
trap 'runuser -u postgres -- "$CT_HUB_PGBIN/pg_ctl" -D "$CT_HUB_PG_DIR/data" -m fast -w stop' EXIT
runuser -u postgres -- "$CT_HUB_PGBIN/psql" \
  -h 127.0.0.1 -p "$CT_HUB_PG_PORT" -d postgres -v ON_ERROR_STOP=1 \
  -c 'CREATE ROLE commontrace_test LOGIN NOSUPERUSER NOBYPASSRLS;
      CREATE ROLE commontrace_record_reader NOLOGIN NOSUPERUSER NOBYPASSRLS;
      GRANT commontrace_record_reader TO commontrace_test WITH INHERIT FALSE, SET TRUE;'
runuser -u postgres -- "$CT_HUB_PGBIN/createdb" \
  -h 127.0.0.1 -p "$CT_HUB_PG_PORT" -O commontrace_test commontrace_disposable_test
/tmp/commontrace-goal-env/bin/python -m pip install -r hub/requirements.txt pytest pytest-asyncio
HUB_TEST_DATABASE_URL="postgresql+asyncpg://commontrace_test@127.0.0.1:$CT_HUB_PG_PORT/commontrace_disposable_test" \
  /tmp/commontrace-goal-env/bin/python -m pytest \
  hub/tests/test_memory_records.py hub/tests/test_rest.py -q
```
