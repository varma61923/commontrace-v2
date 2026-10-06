# Operations, troubleshooting and migrations

## Deploy and observe

Use the tested deployment assets in [deploy/README.md](../deploy/README.md) and
[hub/DEPLOYMENT.md](../hub/DEPLOYMENT.md); those cover local/staging/production
setup, migrations, TLS, replicas, connection budgets and restoration drills.
CI validates assets and never applies infrastructure. Keep development on
loopback. The local HTTP gateway defaults to at most 128 active connections;
`make_http_server(..., max_connections=...)` configures the positive limit. Idle
connections retain a slot until timeout; excess connections close without starting
a waiting worker. Deploy a reverse proxy with appropriate client retry/admission
policy when public traffic exceeds that budget. Store tokens/keys in deployment secrets, not CLI history or the repo.

Hub `/healthz` checks the process and `/readyz` checks database readiness.
Existing telemetry, request metrics, webhook delivery state and optional
ServiceMonitor/SLO alerts provide operational signals. Performance measurements
are workload-specific; do not translate a local warm p95 into a global SLA.

## Diagnose common failures

| Symptom | Check |
| --- | --- |
| Empty retrieval | Root/space, approved status, scopes, validity dates, source eligibility and configured floor; inspect raw evidence before changing the scorer. |
| Stale/corrupt optional index | Rebuild through the supported index command; retain authoritative source files/SQLite. Do not trust a generated index after source drift. |
| Lock timeout | Find the active writer and check filesystem locking support. Acquisition defaults to 30 seconds; protected-block execution is not timed out. |
| Invalid settings | Inspect the named config field; nonfinite/out-of-range retrieval settings fall back safely when loaded and are rejected when explicitly configured. |
| Unauthorized or forbidden | Distinguish missing/revoked API key, org region, API-key scope and person role/capability. Do not retry permanent authentication failures. |
| Rate limited | Respect Retry-After and shared pacing. Reduce concurrency or wait; retries have budgets and jitter. |
| Encryption failure after rotation | Confirm CURRENT key and configured PREVIOUS keys. Malformed envelopes fail with EncryptionError; do not bypass authentication or log ciphertext/keys. |
| Webhook retry | Check bounded delivery state, endpoint validation and connectivity; stored errors omit credential-bearing transport messages. |
| PostgreSQL pool pressure | Check replica × connection budget and pending work before increasing pool size. |

Use generated [environment reference](environment.md) for source locations;
never dump the process environment when diagnosing credentials. Tests for local
SQL enforce read-only statements, result bounds and progress-handler deadlines.

## Upgrade and recover

Back up raw memory and SQLite databases consistently before upgrading. Writable
conversation opens apply idempotent migrations; legacy turns stream and fact
hashes backfill in bounded batches. Belief repair is transactional and temporary
staging is removed on completion/failure. Keep raw provenance and historical
facts; do not delete old facts to resolve an update.

Hub upgrades use the existing Alembic migration chain; see deployment guidance.
Restore into a new database, validate readiness and tenant/source evidence, then
repoint configuration. Do not run test fixtures against a restored production DB.

Batch contributions return ordered independent outcomes: a later refusal does
not roll back prior successful items. Persist distinct idempotency keys before
retrying contributions. Batch deletions permanently remove owned chains and
must not automatically replay ambiguous transport failures.
