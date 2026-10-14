# Enterprise deployment and maintenance

CommonTrace supports an authenticated, tenant-scoped Hub on PostgreSQL and a
local runtime with durable ingestion and memory-maintenance jobs. Use the
[deployment runbook](../hub/DEPLOYMENT.md) for TLS, runtime/migration roles,
shared rate limits, connection budgets, secrets, retention/legal holds and
restore drills. Use the [distribution guide](distribution.md) for remote MCP
OAuth discovery, customer-managed identity providers and generated OpenAPI
clients. The browser console currently signs in with an API key; external MCP
OAuth login and a CommonTrace-hosted browser login are different contracts.

## Verified database security before startup

Compose, plain Kubernetes manifests and Helm now explicitly set
`HUB_REQUIRE_RLS=true`. A required security check refuses startup if database
connectivity, permissions or catalog inspection prevent verification. An unknown
security state cannot silently become an accepted deployment. Optional local
diagnostics remain available when this requirement is explicitly disabled.
`HUB_ALLOW_RLS_BYPASS=true` does not override required enforcement; an intentional
bypass must also explicitly disable `HUB_REQUIRE_RLS`.

Apply Alembic migrations using the database owner, then serve using the separate
runtime role with neither superuser nor `BYPASSRLS` privileges. Keep the runtime
role out of owner-role memberships. Store both credentials in deployment secrets.
The startup check also rejects policy-backed tables whose row security was
disabled and owner-role connections that bypass policies without `FORCE ROW LEVEL
SECURITY`. Installed policies alone are insufficient: PostgreSQL keeps them when
row security is disabled. This inspection covers policy-backed tables, not every
possible table or the correctness of each policy expression.
The shipped migrations establish the policy-backed tenant tables; application
SQL predicates and authentication still cover the complete service boundary.

Before routing customer traffic:

1. Apply migrations and run `python -m alembic -c hub/alembic.ini check`.
2. Start the Hub with the runtime role and `HUB_REQUIRE_RLS=true`.
3. Verify `/healthz`, `/readyz`, an authenticated write/read, and a second
   tenant's inability to access that record.
4. Exercise key revocation, IdP/user linkage, retention holds and deletion in
   staging with representative permissions.
5. Restore a backup into a separate database and repeat the verification.

CI validates code and disposable deployments. Your cloud account, TLS, network
policies, secret-store permissions, regional constraints and backup recovery
require a rehearsal in your own environment.

## Durable local workers

```bash
commontrace jobs run --watch --lease-seconds 600 --dest /srv/commontrace
commontrace jobs list --status dead --json --dest /srv/commontrace
commontrace jobs retry JOB_ID --dest /srv/commontrace
```

Workers renew their claim every one-third of the configured lease while a
handler runs. Claims carry a private, unique token; replacing a claim or manually
retrying work invalidates the old token. The worker's completion, failure and
renewal cannot overwrite another attempt's state. Expired workers cannot renew
themselves back into ownership. Repeated crashes consume the attempt budget and
park the job as `dead`, so broken work cannot retry forever.

Handlers remain **at least once**: lease fencing protects queue state, not an
external side effect that already occurred. Use source identities, durable
idempotency keys or transactional writes for those effects. A worker that loses
its lease withholds its result, and the next attempt may repeat the work.

Queue payloads must be JSON objects of at most 256 kB. Results must serialize to
at most 64 kB; larger results should be written to a protected artifact and return
its path. Oversized results become readable job failures instead of corrupting
the stored JSON. Existing `memory/jobs.db` files gain the claim-token column
transactionally on open, without discarding jobs.

Private claim tokens do not appear in the CLI job JSON. Python worker integrations
using `claim()` should pass `job.lease_token` to `renew()`, `complete()` and
`fail()`. Omitting it preserves the existing low-level operator override; automatic
workers always use fencing. Stop workers before an upgrade and use SQLite's
[backup API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)
for a consistent queue snapshot, or checkpoint and copy the closed database.
Copying a live database and its WAL independently is not a consistent backup.
Do not run mixed old/new worker versions against one root: old workers do not
understand fenced claims.

Monitor `commontrace_jobs_lease_lost` and `commontrace_jobs_renewal_failed` alongside
the existing job counters. Investigate database contention, filesystem health,
process pauses and worker restarts before increasing leases. A worker stopped
normally drains its current job; a hard-killed worker is recovered after expiry.

## Long-session summary performance

Rolling extractive summaries retain twelve candidate sentences, plus the requested
output prefix. Selection now stops after that needed prefix instead of ordering
the entire candidate bank. It preserves the previous greedy choices, chronological
output, temporal/novelty scores, vocabulary and evidence hash chain. Persisted
summary state remains equivalent. Transient candidate collection still scales
with the newly processed input; this change removes quadratic ranking work, not
all input-proportional memory or I/O.

Run the paired production workload:

```bash
python -m benchmarks.session_summary --turns 1000 --trials 3 --check
```

Both arms use real SQLite paging, evidence hashing and summary persistence.
The reference reproduces the old full ranking; the optimized arm selects the
needed prefix. Equality checks include the entire persisted rolling state and
delivered summary, excluding only the update timestamp. CI requires equality and
a 10x improvement on this named long-session workload. Short sessions, model
summaries, remote requests and other systems have different performance profiles.

See [research and measured release scope](research/enterprise-maintenance.md)
for competitive evidence, acceptance contracts and measurement boundaries.
