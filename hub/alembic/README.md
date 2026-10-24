# Hub migration history

`versions/` is one append-only upgrade chain. Existing databases may record any
of its revision IDs in `alembic_version`. Add a new revision for schema or data
changes; retain existing IDs, parent links and migration bodies.

`history.json` freezes the reviewed history through `ff58bac2d4f6`. Its hashes
make accidental rewrites detectable. New revisions do not need to be added to
this historical snapshot. Changing the snapshot is an explicit compatibility
review, not a way to make a failing history check pass.

## Generate future revisions

Use the shared template instead of copying an old migration:

```sh
python -m alembic -c hub/alembic.ini revision -m "describe the schema change"
```

Keep migration operations self-contained. Do not import current ORM models or
application helpers into revision files: replay must remain stable as runtime
code evolves. Consolidate declarations in the template; keep historical SQL
and backfill behavior frozen in each revision.

For new tenant tables choose policy semantics explicitly:

| Policy family | Required behavior |
| --- | --- |
| Strict tenant | Require `org_id::text = current_setting('app.org_id', true)` for both `USING` and `WITH CHECK`; enable and force RLS. Missing tenant context denies access. |
| Legacy operator | Some existing tables deliberately permit an empty tenant setting for operator work. Do not copy this allowance onto new tables without reviewing every unscoped caller. |
| Public read | Add a separate `FOR SELECT` policy. Never put public sharing conditions in a `FOR ALL` policy: that also grants UPDATE/DELETE visibility. |

## Apply and verify

Use a migration-owner connection, distinct from the serving role:

```sh
python -m hub.migrate plan
python -m hub.migrate upgrade --apply
python -m hub.migrate verify
```

The operator serializes its operations with a PostgreSQL advisory lock. Avoid
concurrent direct Alembic runs, which do not acquire that operator lock.
`verify` checks ORM-visible schema drift; separately verify policies, functions,
triggers and tenant behavior under a non-bypass role.

Two historical revisions (`7c4a1d92e5b8`, `8f2b40c17ade`) use autocommit blocks
for concurrent indexes. The entire chain is not one atomic transaction. Inspect
the recorded revision and index state before retrying interrupted migrations.
Full-chain offline `upgrade head --sql` is currently unsupported: those
revisions and `a1c7e4f93d2b` inspect live columns.

The historical corpus functions use an explicit `pg_catalog, public` search
path. Use the supported public-schema deployment; a custom-schema replay is
not an isolated substitute for a fresh public-schema migration test.

Rollback requires an explicit ancestor, acknowledgement of data loss and a
restore-tested backup via `hub.migrate`. Audit credential redaction in
`ff58bac2d4f6` is irreversible even after a downgrade.

## Future baseline consolidation

A fresh-install baseline can create final columns, bigint counters, partial
indexes and transparency frontier directly. It must also preserve generated
columns, constraints, functions/triggers, final RLS policies and forced RLS.
Current `Base.metadata.create_all()` is not a replacement for migration history.

Before retiring history, inventory deployed revisions and define a supported
upgrade floor. Compare a baseline-created disposable database with one upgraded
through the historical chain, including SQL objects and tenant behavior. Test
supported older revisions with populated counters, votes, memory streams,
transparency heads and audit data. Preserve their backfills and repairs.
Do not stamp an existing database to a new baseline without proving parity.
