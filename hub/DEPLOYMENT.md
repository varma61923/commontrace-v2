# Deploying the CommonTrace Hub

What an operator needs to run the Hub for real, as opposed to the
local-checkout instructions in [`hub/README.md`](README.md).

> **Verification status, stated up front.** The application is exercised
> against a real PostgreSQL 16 instance by `hub/tests/` (69 tests, including
> tenant isolation), on Python 3.10/3.11/3.12, and CI additionally applies
> every migration to an empty database and runs `alembic check` for drift.
>
> The **container image** is built and started in CI (`docker-build` job):
> it builds, the container comes up, `/healthz` serves, and `/readyz`
> correctly returns 503 with no database reachable.
>
> The **compose stack is exercised end to end in CI** (`compose-stack` job):
> it brings up the documented stack, waits on `/readyz`, asserts the
> migrations created every table, provisions two organizations through the
> operator CLI, then drives the running server over real HTTP — every MCP
> tool, an unauthenticated request refused with 401, and cross-tenant reads
> refused — restarts the app and checks the data survived, and asserts the
> logs are structured JSON containing no API key or database password.
>
> One honest caveat remains: none of this has run against a
> production-*like* environment — real TLS termination, a managed Postgres,
> more than one replica. Do a rehearsal deploy before a client's data lands,
> and run `python -m hub.smoke` (§12) against it.

---

## 1. What you need

| Requirement | Notes |
|---|---|
| PostgreSQL 13+ | 16 is what's tested. Managed (RDS/Cloud SQL/Neon/…) is fine and recommended — you want its backups. |
| A container runtime **or** Python 3.10+ | The image is optional; `pip install -r hub/requirements.txt` + `python -m hub.main` works too. |
| A secret store | For `HUB_DATABASE_URL` and issued API keys. Not a `.env` file in your repo. |
| TLS termination | The Hub speaks plain HTTP. Put it behind your load balancer / ingress — API keys travel in an `Authorization` header and must not cross the network in cleartext. |

No Redis, no message broker, no object storage. State lives entirely in
Postgres.

## 2. Configuration

Every setting is an env var, all documented in
[`hub/.env.example`](.env.example). The only one with no default is
`HUB_DATABASE_URL` — the server refuses to start without it rather than
guessing a connection string.

Sizing note: `HUB_DB_POOL_SIZE` (default 10) is **per replica**. The product
`HUB_DB_POOL_SIZE × replicas` must stay comfortably under your Postgres
`max_connections`, or a rolling deploy will exhaust connections while old
and new replicas overlap.

## 3. Migrations

Run them as a **separate step before** rolling out new app instances, never
on container start:

```bash
python -m alembic -c hub/alembic.ini upgrade head
```

`docker-compose.yml` models this with a one-shot `migrate` service the `hub`
service waits on (`condition: service_completed_successfully`). On
Kubernetes, use a Job or an init-container that runs once — not an
init-container on every pod, which reintroduces the race.

Why it matters: N replicas each running `upgrade head` on boot race the same
DDL. Alembic takes a lock, so you usually get a slow deploy rather than a
corrupted schema — but "usually" is not a deployment strategy.

## 4. Health probes

Two endpoints, answering deliberately different questions. Wiring them to
the wrong probe causes real outages:

| Endpoint | Question | Checks DB? | Probe to attach |
|---|---|---|---|
| `GET /healthz` | "Is this process alive?" | **No** | liveness |
| `GET /readyz` | "Should I get traffic?" | **Yes** (`SELECT 1`) | readiness |

Both are unauthenticated (a load balancer has no tenant credentials).

`/healthz` must not check the database on purpose: a failing liveness probe
means *restart me*, so making it DB-dependent turns a 30-second database
blip into a simultaneous restart of every replica. `/readyz` returning 503
correctly takes an instance out of rotation and puts it back when the
database recovers.

Kubernetes sketch:

```yaml
livenessProbe:
  httpGet: { path: /healthz, port: 8420 }
  periodSeconds: 30
readinessProbe:
  httpGet: { path: /readyz, port: 8420 }
  periodSeconds: 10
```

## 5. Running it

**Compose (evaluation / single host):**

```bash
docker compose up --build -d
docker compose run --rm hub python -m hub.manage create-org "Acme Corp"
docker compose run --rm hub python -m hub.manage issue-key <org_id> 90   # 90-day expiry
docker compose run --rm hub python -m hub.manage set-plan <org_id> team  # entitlements
```

A new org lands on `free` (1,000 traces, 20 commons queries/month). That is
deliberate: the migration that added plans defaults every existing org to
the smallest one, because a migration that silently upgrades every customer
gives the product away. Move orgs with `set-plan`; see their meters with
`python -m hub.manage usage`.

**Without containers:**

```bash
pip install -r hub/requirements.txt
export HUB_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/commontrace_hub
python -m alembic -c hub/alembic.ini upgrade head
python -m hub.main
```

Clients connect to `https://<your-host>/mcp` with
`Authorization: Bearer <api-key>`.

## 6. Scaling, and the one thing that doesn't scale horizontally yet

The Hub is stateless apart from Postgres, so replicas scale out normally —
**with one documented exception**:

> `contribute_trace` rate limiting (`hub/abuse.py`) is an **in-process token
> bucket**. It resets on restart and does not coordinate across replicas, so
> N replicas allow roughly N× the configured rate.

That is a known MVP limitation, not a bug to be surprised by. Until it moves
to a shared store (Redis `INCR`+`EXPIRE`, or a Postgres-backed bucket), your
options are: run a single replica, set `HUB_RATE_LIMIT_PER_MINUTE` to
`desired ÷ replicas`, or enforce the real limit at your ingress.

Search scales differently and is fine: matching goes through the
`ix_traces_search_vector_gin` full-text index rather than a sequential scan
(measured at 50k traces in one org: ~113 ms sequential scan → ~9 ms index
scan on a selective query).

## 7. Observability

Logs are JSON on stdout, one object per line, ready for any aggregator.
Every request gets a correlation id — taken from an inbound `X-Request-ID`
if the client sent one, otherwise generated — which appears on every log
line for that request and is echoed back in the response header, so a user
report can be traced to exact log lines.

Request logs deliberately record method, path, status, and duration only —
never query strings or bodies, which carry customer trace content. API keys
are never logged in any form; audit entries attribute actions to the key's
non-secret prefix.

Set `HUB_LOG_LEVEL` (default `INFO`).

## 8. Audit trail

Consequential actions are recorded in the `audit_log` table: every write via
the MCP tools (`contribute_trace`, `vote_trace`, `amend_trace`) and every
`hub/manage.py` admin command, including the destructive ones.

Two properties worth knowing:

- **Audit rows survive an org purge.** `audit_log.org_id` is intentionally
  *not* a foreign key with `ON DELETE CASCADE` — purging an organization
  must not erase the record that the purge happened.
- **They contain no customer content.** Summaries are bounded metadata
  ("`title_len=42 n_tags=3`"), never trace bodies. Since the rows outlive a
  purge, putting content in them would defeat the purge.

Ordinary reads are not written to `audit_log` (high volume, low signal);
read visibility comes from the request logs.

## 9. Backup and restore

Nothing to back up but Postgres — take your provider's automated backups
(or `pg_dump`) and, more importantly, **test a restore**. An untested backup
is a hypothesis.

The one Hub-specific note: API keys are stored only as argon2 hashes, so a
restore does **not** recover any raw key. Keys that clients hold keep working
after a restore (the hash is what's compared); but there is still no way to
re-display a key anyone has lost — rotate instead
(`python -m hub.manage rotate-key <key_id>`).

## 10. Security checklist before a client's data lands

- [ ] TLS terminated in front of the Hub (API keys are bearer credentials).
- [ ] `HUB_DATABASE_URL` from a secret store, not a file in the repo.
- [ ] Postgres not publicly reachable; Hub reaches it over a private network.
- [ ] API keys issued with an expiry (`issue-key <org_id> <days>`) rather
      than never expiring.
- [ ] Rate limiting understood per §6 (or enforced at the ingress).
- [ ] Backups on, and a restore actually rehearsed.
- [ ] Read [`DATA_RETENTION.md`](../DATA_RETENTION.md) — deletion is
      operator-CLI-only by design.
- [ ] If the cross-org commons is not wanted for this deployment, do not
      call `commons-seed` and tell your orgs not to `share_trace` — it is
      opt-in per trace (§13) and stays empty unless something is shared
      into it. `commons_overlap` on an empty commons returns 0% with a
      note saying why, not an error.

## 11. Known limitations (deliberate, documented)

| Limitation | Where |
|---|---|
| Rate limiting is per-process | §6, `hub/abuse.py` |
| Auth is API-key-only; no OAuth/JWT, no per-key scopes | `hub/README.md` |
| No self-service data deletion (operator CLI only) | `DATA_RETENTION.md` |
| Cross-org commons is lexical-match only; recall against paraphrased failures is ~11% (floor, not estimate) | `commons/eval/RESULTS.md` |
| `CO_RETRIEVED` trace relations not computed | `hub/README.md` |
| No payment/billing integration — `hub/plans.py` enforces entitlements, no invoicing | §13 |
| No production-like rehearsal (TLS, managed PG, multi-replica) | top of this file |

---

## 12. Verify the deployment you just made

CI proves the code and the compose stack work. It cannot prove *your*
deployment works — your TLS terminator, your managed Postgres, your ingress,
your secret store. That gap is where deployments actually fail, so verify it
directly:

```bash
pip install "commontrace[hub-sync]"

python -m hub.smoke \
  --url https://your-hub-host/mcp \
  --api-key ct_live_...            \
  --other-api-key ct_live_...        # a SECOND org's key
```

It exercises every MCP tool against the live server, confirms an invalid
key is refused, and — with `--other-api-key` — confirms one tenant cannot
read, vote on, or amend another's trace. Exit code 0 means every check
passed; each check prints its own line, so a failure names the property that
broke rather than making you bisect.

**Pass the second key.** Without it the isolation checks are skipped, and
isolation is the property most worth proving before a customer's data lands
in a deployment. Issue a throwaway org for the purpose:

```bash
python -m hub.manage create-org "smoke-check-throwaway"
python -m hub.manage issue-key <org_id> 1        # expires tomorrow
```

The run writes a small number of traces tagged `commontrace-smoke` under the
calling org and nothing else. It prints the `purge-trace` command to remove
them. Running it against production is safe; running it against a fresh
deployment before you hand out the first customer key is the point.

### What a failure means

| Message | Cause |
|---|---|
| `could not reach <url>` | DNS, ingress, or the service is down. Check `/readyz` directly. |
| `HTTP 404 at <url>` | Wrong endpoint path. It defaults to `/mcp` (`HUB_STREAMABLE_HTTP_PATH`). |
| `rejected the API key (HTTP 401)` | Key is wrong, revoked, or expired. |
| `HTTP 5xx` | Reachable but failing. Check the container logs and `/readyz`. |
| A named `[FAIL]` check | The server is up but a behavioural guarantee broke. Do not hand out keys. |

## 13. Running this for a single organization, privately, on your own fleet

Everything above works unchanged for one org running its own Hub for its
own fleet — that is the simplest deployment shape this server supports,
not a stripped-down mode. Tenant isolation, migrations, health probes,
backups, and the security checklist are identical whether one org uses
the Hub or a thousand do. Two things are specific to running it alone:

- **The cross-org commons defaults to inert, and can be removed outright.**
  Left at its default, `share_trace` is per-trace opt-in and nothing is
  shared unless someone calls it — `commons_overlap` on an empty commons
  costs nothing and returns 0% with a note explaining why, never an error.
  That is "nobody happens to use it." If the requirement is stronger than
  that — an internal-only deployment where the commons must not exist,
  full stop, regardless of what any org's traces do — set
  `HUB_COMMONS_ENABLED=false`. `share_trace`, `unshare_trace`, and
  `commons_overlap` are then absent from the MCP tool surface entirely
  (an unknown-tool error to any client that tries), not merely refused;
  `python -m hub.smoke`'s tool-surface check reflects whichever mode the
  server is actually running in. Skip `commons-seed` either way if there
  is only ever going to be one org — there is no other org for it to
  compare against.
- **Entitlements can be ignored.** A new org lands on the `free` plan
  (1,000 traces, 20 commons queries/month — §5). If that is not the
  point of running your own Hub, `python -m hub.manage set-plan <org_id>
  scale` once and move on; there is no billing system watching this, it
  is a self-imposed ceiling you can raise for yourself.

What still applies in full: run the migration as its own step (§3), wire
both health probes correctly (§4), understand that `contribute_trace`
rate limiting is per-process so a single replica is the simple case
rather than a limitation to work around (§6), take backups and rehearse a
restore (§9), work through the security checklist (§10) before any real
data lands even if "the client" is your own team, and run
`python -m hub.smoke` against your own deployment (§12) — omit
`--other-api-key` if there genuinely is only one org, but issue a
throwaway second org and pass it if you want the tenant-isolation checks
to run at all; they do not run without it.
