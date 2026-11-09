# Production readiness and operations

UniMem ships a local runtime and a separate multi-tenant Hub. Validate the
profile you deploy: an offline demo, a successful build or a local benchmark
does not establish a production availability or capacity guarantee.

## Release a verified artifact

1. Run CI on the exact commit through a push to `main` or a manual CI run on the
   intended commit. A pull-request merge preview is not evidence for a different
   tagged commit. Wait for the workflow to finish successfully.
2. Use the Release workflow's default dry run to build and inspect artifacts.
   The Python wheel is installed into a clean environment and must initialize a
   store, write a fact, and recall that fact within a shared 60-second deadline.
   Generated TypeScript is compiled, packaged, installed and smoke-tested too.
3. Only tag a version matching `pyproject.toml`. Publication requires successful
   CI for the actual checked-out commit, including annotated tags. The newest
   matching trusted run must be successful; missing, pending, failed and
   cancelled evidence refuses publication. API errors also refuse publication.
4. Configure the repository's PyPI/npm trusted publishers and protected release
   environments. Keep required reviewers and branch protection enabled through
   the hosting platform. Repository code cannot enforce account-level settings.

The CI lookup uses a read-only Actions token, a bounded response and a network
timeout. It does not follow redirects with credentials. No package publication
or production deployment is performed by local verification commands.

The local gateway bounds active connections and request bodies. Its
`make_http_server(..., request_timeout=10)` timeout is a total input budget for
each request's headers and body; trickling bytes cannot extend it. Persistent
connections receive a new input budget for each request. Application execution
uses its own operation limits, and response socket writes retain their normal
timeout. Size reverse-proxy limits and client upload expectations accordingly.

## Bootstrap the Hub

- Provision PostgreSQL and a migration owner separately from the serving role.
  The serving role must be `NOSUPERUSER NOBYPASSRLS`, without schema creation
  privileges. The Compose initialization SQL in
  [10-runtime-role.sql](../hub/postgres-init/10-runtime-role.sql) shows the required
  grants and default privileges; adapt role names to your managed database.
  Provision the shared rate-limit table from that initialization SQL as the
  migration owner too; do not grant the serving role DDL rights to compensate
  for missing setup.
- Use a secret manager to provision `HUB_DATABASE_URL`, a shared
  `HUB_API_KEY_PEPPER`, `HUB_LEDGER_SIGNING_KEY`, and encryption keys. Provision
  `HUB_CONSOLE_SECRET`, `HUB_ADMIN_TOKEN` and `HUB_METRICS_TOKEN` only for the
  surfaces you enable. Never commit populated Secret manifests.
  The chart refuses known credential fields in `config` so they cannot be
  accidentally rendered into a ConfigMap; supply them through `existingSecret`.
- Set `HUB_REQUIRE_RLS=true` and `HUB_REQUIRE_API_KEY_PEPPER=true`. Do not bypass
  a failed RLS check to make the service start. Keep key/encryption material
  stable across replicas and restarts; retain prior decryption keys during a
  verified rotation.
- Run the migration plan with the migration owner's connection, then apply it:

  ```bash
  python -m hub.migrate plan
  python -m hub.migrate upgrade --apply
  python -m hub.migrate verify
  ```

- Start the Hub with the runtime role. Terminate TLS at a trusted ingress and
  restrict direct backend access. Set `HUB_TRUSTED_PROXY_HOPS` to the actual
  trusted proxy count. `HUB_ALLOW_INSECURE_HTTP=true` permits the internal
  ingress-to-process connection; it is not a public-TLS replacement.

Compose is loopback-published and intended for local integration. It forwards
the documented security values from its environment, including pepper,
encryption/signing keys and metrics credentials. Set distinct database owner
and application passwords; its demonstration defaults are not production
credentials. For multi-replica deployment use the Helm profile or equivalent
managed orchestration and `HUB_RATE_LIMIT_BACKEND=postgres`.

## Deploy and roll back safely

The Helm chart requires an existing Secret and an image reference. Prefer an
immutable digest:

```bash
helm template unimem deploy/helm/unimem-hub \
  --set image.repository=registry.example/unimem-hub \
  --set image.digest=sha256:YOUR_VERIFIED_64_CHARACTER_DIGEST \
  --set existingSecret=unimem-hub-secrets
```

Replace the example digest before rendering; invalid digests are refused.
Rendering is inspection, not deployment. Supply production-specific network
policy, ingress/TLS, database limits and topology before applying it.

Budget database connections for `(pool size + overflow) × (maximum replicas +
one rolling-update surge)`, plus migrations, listeners, monitoring and operator
reserve. Set `postgres.maxConnections` to the portion available for serving;
the chart refuses a serving budget above that value.

Startup probes allow up to three minutes before liveness restarts a booting
replica. `/healthz` is process liveness and does not query PostgreSQL. `/readyz`
checks the dependency path with a two-second cooperative deadline by default;
failures return 503. `HUB_READYZ_TIMEOUT_SECONDS` must remain below the rendered
readiness probe timeout. Metric scrapes use their own local budget and cannot
consume readiness tokens or require a database round trip.

Keep `terminationGracePeriodSeconds` above `HUB_GRACEFUL_SHUTDOWN_SECONDS`;
the chart enforces this relationship. A zero application grace cancels pending
background work immediately. Application startup failure and shutdown errors
still dispose the SQLAlchemy engine after inner lifecycle cleanup. Roll a
secret change explicitly: the chart hashes ConfigMaps, not secret values.

Retain the previous image digest and configuration for application rollback.
Check schema compatibility before rolling back a binary. Destructive migration
rollback is separate: `hub.migrate` requires explicit data-loss acknowledgement
and a separately verified backup. Do not substitute a binary rollback for a
database restore.

## Observe user outcomes and service health

Enable authenticated metric scraping and the optional alert rules. Shipped
starting objectives are 99.9% request availability and 95% below 250 ms; measure
them under your workload before adopting a contractual SLO. Recording rules
exclude health/readiness/metrics traffic so successful probes cannot dilute
user failures. Idle periods do not count as slow requests.
Set `metrics.tokenSecretKey` to the key in the existing Secret containing the
scrape token. The chart maps that key to `HUB_METRICS_TOKEN` in the server and
uses the same key in ServiceMonitor authorization, including custom key names.

`HubNoTraffic` also detects absent business-request series, even when probes
continue. Enable that alert only where business traffic or canaries are expected
continuously. Monitor scrape failures and infrastructure health through your
platform as well. Native `promtool` tests in CI evaluate idle and failing-user
traffic, and Helm tests render the optional monitoring resources.

Record p50/p95/p99 latency, error/timeout rates, rejected admissions, database
pool saturation, CPU/RSS and disk growth at the expected corpus size and peak
concurrency. Retain results for the exact image and configuration. Performance
figures in [the research report](performance-research.md) are workload-specific.

For an authentication and rate-limiter load smoke, use a disposable database:

```bash
python -m hub.bench_concurrency --clients 1,4,8 --requests 64 --backend both --json
```

This exercises key verification and limiter decisions; it excludes network/TLS
and application queries and does not establish serving capacity.

## Back up and rehearse recovery

Define acceptable data loss (RPO), recovery time (RTO), ownership and retention
before launch. Use encrypted, access-restricted backups and managed PostgreSQL
PITR where required. A backup file alone is not proof of recovery.

Restore into a new isolated database using operator-managed libpq service and
credential files; never restore a drill over a live serving database:

```bash
PGSERVICE=unimem-backup pg_dump --format=custom --no-owner --no-acl --file=hub.backup
pg_restore --exit-on-error --no-owner --no-acl --dbname=service=unimem-restore hub.backup
```

Reapply the environment's runtime grants, verify migration head and forced RLS,
and check a non-superuser's cross-tenant denial. Then verify stored records,
search/recall, outcome recording and governed erasure. Retain encryption/signing
keys separately under the same recovery controls; a database restore cannot
reconstruct destroyed keys. Record recovery duration and failures, then remove
the isolated drill resources according to the retention policy.
