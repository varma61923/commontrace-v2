# Deploying the CommonTrace Hub

What an operator needs to run the Hub for real, as opposed to the
local-checkout instructions in [`hub/README.md`](README.md).

> **Verification status, stated up front.** The application is exercised
> against a real PostgreSQL 16 instance by `hub/tests/` (767 tests, tenant
> isolation among them) on Python 3.10/3.11/3.12, alongside 939 client-side
> tests. CI additionally applies every migration to an empty database, runs
> `alembic check` for drift, and proves an interrupted `CONCURRENTLY` index
> migration can be retried.
>
> The **container image** is built and started in CI (`docker-build` job),
> and the **compose stack end to end** (`compose-stack` job): it brings up
> the documented stack, waits on `/readyz`, asserts the migrations created
> every table, provisions two organizations through the operator CLI, drives
> the running server over real HTTP — every MCP tool, an unauthenticated
> request refused with 401, cross-tenant reads refused — restarts the app
> and checks the data survived, and asserts the logs are structured JSON
> containing no API key or database password.
>
> The following were additionally rehearsed by hand against a live
> deployment, because each is something a client depends on and none of them
> is proven by a unit test:
>
> | Rehearsed | Result |
> |---|---|
> | Migrations onto an empty database, then `alembic check` | 13 revisions applied, no drift |
> | Image built and run directly | Serves `/healthz`, `/readyz`, `/metrics`; runs as uid 10001, not root |
> | Container `HEALTHCHECK` | Reports healthy, and honours a non-default `HUB_PORT` |
> | Container restart | Data intact; logs JSON with no key or password in them |
> | Compose stack from clean, via the documented commands | Both services healthy; `hub.smoke` 17/17 including tenant isolation |
> | `rotate-key` | Old key refused on the next request; new key passes the full smoke check |
> | `purge-org` | Target org fully removed, the other tenant's traces untouched |
> | Self-service deletion gates | Refused when too early, on a wrong token, and with no pending request |
> | Backup → restore → serve (§9) | Row counts match; a key the client already held still authenticates; 17/17 smoke |
>
> **The caveat that remains, stated plainly:** none of this has run against
> a production-*like* environment — real TLS termination, a managed
> Postgres, more than one replica, or sustained concurrent load. Single-
> process rate limiting (§6) is a known limit of that shape. Do a rehearsal
> deploy before a client's data lands, and run `python -m hub.smoke` (§12)
> against it.

---

## 1. What you need

| Requirement | Notes |
|---|---|
| PostgreSQL 13+ | 16 is what's tested. Managed (RDS/Cloud SQL/Neon/…) is fine and recommended — you want its backups. |
| A container runtime **or** Python 3.10+ | The image is optional; `pip install -r hub/requirements.txt` + `python -m hub.main` works too. |
| A secret store | For `HUB_DATABASE_URL` and issued API keys. Not a `.env` file in your repo. |
| TLS termination | The Hub speaks plain HTTP. Put it behind your load balancer / ingress — API keys travel in an `Authorization` header and must not cross the network in cleartext. |
| `HUB_ALLOW_INSECURE_HTTP=true` if `HUB_HOST` isn't loopback | The Hub refuses to start bound to a non-loopback interface (e.g. `0.0.0.0`, needed for container/pod networking) unless this is set — a deliberate acknowledgment that a proxy in front is terminating TLS, not a guess the Hub makes about your network. Never set it because the Hub itself is meant to be reached directly without a proxy. |

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
| `GET /metrics` | "What is it actually serving?" | **No** | Prometheus scrape |

All three are unauthenticated (a load balancer and a metrics scraper carry
no tenant credentials).

### The customer console

Set `HUB_CONSOLE_SECRET` and the Hub serves a console at `/app` for your
**customers**, authenticated by their own API key and scoped to their own
organisation. Four pages: what their fleet has captured and how it is using
its plan, whether the memory is working (with the validity verdict rendered
*above* the effect sizes), the corpus searched the way their agents search
it, and their Knowledge Base proposals and credit.

```bash
HUB_CONSOLE_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
```

Operationally, four things to know:

- **It is read-only.** Nothing on it changes state, which is why it carries
  no CSRF token — there is no state-changing request for a forged one to
  trigger. Everything a customer can change goes through MCP or the CLI,
  where it is authenticated and audited.
- **Revoking a key ends the browser sessions it opened**, checked on every
  request. `revoke-key` is a working emergency stop for the console too.
- **Rotating `HUB_CONSOLE_SECRET` signs every customer out.** That is the
  blunt instrument if you suspect a session leak; it needs no database
  change and takes effect on restart.
- **Unset means the route does not exist.** A deployment that has not opted
  in has nothing to probe.

This secret is deliberately **not** `HUB_ADMIN_TOKEN`. That token is your
operator credential; this one signs customer sessions. One value doing both
means one leak compromises both surfaces at once.

### The operator console

Set `HUB_ADMIN_TOKEN` and the Hub also serves an operator console at
`/admin`: every organization and what it is using against its plan, per-org
key state and expiry, quarantined traces, retrieval miss rate, recent
audited actions — and the Knowledge Base, which is where the only exchange
between an org and anything outside it actually happens.

**Orgs never exchange anything with each other.** A fleet's traces stay
private to that fleet; there is no tool on this Hub that shows one org
another's data. The Knowledge Base is the single surface where content
crosses an org boundary, and it does so through a person: an org *consults*
it by sending a signature (never its text), and an org *proposes* an entry
that stays invisible to everyone until an operator accepts it here. An
accepted proposal is published under the operator's org, so what other orgs
read is operator-curated substrate knowledge rather than a customer's
record — and the proposing org earns a permanent query-allowance credit,
which is the whole incentive. See `hub/commons.py` for why direct
org-to-org sharing was designed out rather than never built.

Unset — the default — **the routes are not registered at all**, so a
deployment that has not opted in returns 404 rather than 401. Authentication
is HTTP Basic (the username is ignored; the password is the token, compared
in constant time), rate limited by client address before the credential is
even checked.

**What it will and will not do is decided by reversibility.**

| Class | Examples | Where |
|---|---|---|
| Reversible moderation | accept/decline a proposal, retract/restore an entry | **The console.** |
| Irreversible or credential-bearing | `purge-org`, `purge-trace`, `issue-key`, `rotate-key` | **The CLI only.** |

Deleting an organization cannot be undone, and issuing a key would put a live
credential into browser history, the page cache, and any screenshot. Those
stay in a terminal that prompts for confirmation. Withdrawing a Knowledge
Base entry, by contrast, is undone by restoring it — and it is high-frequency
work, because a Knowledge Base is only as good as its review queue and a
queue that can only be worked from a terminal does not get worked.

Every console decision writes the same audit row the CLI writes, under the
actor `operator-console`, so "who published this entry" stays answerable.

Because authentication is HTTP Basic, a browser re-sends those credentials on
a cross-site form POST. Every mutating endpoint therefore requires a CSRF
token that is an HMAC of the action **and** its target under the admin
secret — unforgeable without it, and scoped so a token minted to decline one
proposal cannot approve another. Cross-site posts are refused outright where
the browser reports `Sec-Fetch-Site`.

Publishing needs `HUB_OPERATOR_ORG_ID`. Without it the accept action **fails
closed** and shows the CLI command instead: an accepted proposal is published
under the operator's org and never the submitter's, and putting a customer's
id on Knowledge Base content is the one mistake this whole boundary exists to
prevent.

Everything it renders is customer-supplied — trace titles, tags, quarantine
reasons — and it is read by the one session with cross-tenant visibility, so
every value is HTML-escaped on the way out (`hub/tests/test_admin.py` asserts
a trace titled with a `<script>` tag renders inert).

Put it behind the same TLS and network controls as `/metrics`; it is
operator-facing, not public.

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

### Metrics

`GET /metrics` serves Prometheus text format. Three counters:

| Metric | Labels | What it answers |
|---|---|---|
| `commontrace_hub_requests_total` | `method`, `path`, `status` | Traffic and error rate per route. |
| `commontrace_hub_request_duration_ms_total` | `path` | Summed latency; divide by the request count for a mean. |
| `commontrace_hub_rate_limited_total` | `limiter` (`http`, `write`) | **How often you are refusing customers, and by which limiter.** |

The third is the one to alert on. Rate limiting is otherwise invisible
until a customer complains, and a rising `limiter="write"` count is the
signal that `HUB_RATE_LIMIT_PER_MINUTE` is set below what your customers'
fleets actually do. The two limiters are counted separately because they
refuse at different layers: an HTTP 429 comes from the middleware, while a
per-org write refusal is returned *inside* a 200 MCP response and so never
appears in the status-code counter.

There are deliberately **no per-org labels** — no org id, key prefix, or
query text. That keeps the time-series count bounded (one series per route,
not one per customer) and keeps a scrape endpoint, which is a different
trust boundary from an authenticated tool call, free of tenant data. `path`
is bucketed to the routes this app serves, with everything else as `other`,
so an unauthenticated caller cannot inflate cardinality by requesting
arbitrary URLs.

`/metrics` touches no database, so it stays cheap — and readable — exactly
when the database is down. Expose it to your monitoring network, not the
public internet, the same as any `/metrics`.

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

That was one measurement at one corpus size, which shows the index works
and says nothing about *growth*. [`SCALING.md`](SCALING.md) measures the
growth: across a 64× corpus range, no read path grows linearly with a
customer's own history — a selective `search_traces` costs 2.2× for 64×
the data (exponent 0.19), and the worst operator-facing report is 0.74.
Reproduce with `python -m hub.bench_scaling`.

Two caveats that document carries and are worth repeating here. A
deliberately broad query — one matching most of the corpus — does cost
`O(matches)`, because `ORDER BY ts_rank(...)` has to score every match and
no index can serve that ordering; a fleet searching for common words will
find it. And every number is a single query against an otherwise idle
database, so cost per customer under real concurrency is a separate
measurement nobody has made.

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
(or `pg_dump`). An untested backup is a hypothesis, so the rehearsal below
is the part that matters. It has been run against a populated deployment;
the expected results are what it actually produced.

```bash
# 1. Dump.
pg_dump -h $PGHOST -U $PGUSER -d commontrace_hub -Fc -f hub-$(date +%F).dump

# 2. Restore into a NEW database -- never over the live one.
createdb -h $PGHOST -U $PGUSER commontrace_hub_restored
pg_restore -h $PGHOST -U $PGUSER -d commontrace_hub_restored --no-owner hub-$(date +%F).dump

# 3. Verify the schema is current rather than merely present.
HUB_DATABASE_URL=postgresql+asyncpg://.../commontrace_hub_restored \
  python -m alembic -c hub/alembic.ini check     # -> "No new upgrade operations detected."

# 4. Point a Hub at the restored copy and prove it serves, using a key a
#    client already holds -- see the note below for why that is the check.
HUB_DATABASE_URL=postgresql+asyncpg://.../commontrace_hub_restored \
  HUB_PORT=8440 python -m hub.main &
python -m hub.smoke --url http://127.0.0.1:8440/mcp \
  --api-key <existing-client-key> --other-api-key <second-org-key>
```

Step 4 is the one people skip, and it is the only one that proves the
restore is usable rather than merely complete. Row counts matching tells
you the bytes arrived; a passing smoke check tells you a client can still
work.

**API keys survive a restore, and this is worth understanding rather than
assuming.** Keys are stored only as argon2 hashes, so a restore recovers no
raw key — but it does not need to: verification compares the presented key
against the stored hash, so **every key a client already holds keeps working
against the restored database** (verified: an existing key passes the full
smoke check, tenant-isolation checks included, against a freshly restored
copy). What a restore cannot do is re-display a key someone has lost —
there is no path back from the hash. Rotate instead:
`python -m hub.manage rotate-key <key_id>`.

The corollary matters for incident response: restoring an older backup
**resurrects keys revoked after that backup was taken**. If you restore
across a revocation, re-revoke those key ids immediately.

## 10. Security checklist before a client's data lands

- [ ] TLS terminated in front of the Hub (API keys are bearer credentials).
- [ ] `HUB_DATABASE_URL` from a secret store, not a file in the repo.
- [ ] Postgres not publicly reachable; Hub reaches it over a private network.
- [ ] API keys issued with an expiry (`issue-key <org_id> <days>`) rather
      than never expiring.
- [ ] Rate limiting understood per §6 (or enforced at the ingress).
- [ ] Backups on, and a restore actually rehearsed.
- [ ] `HUB_ADMIN_TOKEN` either unset, or set to a real secret with `/admin`
      reachable only from your operator network.
- [ ] `HUB_OPERATOR_ORG_ID` set to your own org if you intend to accept
      Knowledge Base proposals from the console (it fails closed otherwise).
- [ ] `HUB_CONSOLE_SECRET` either unset, or set to a fresh random secret
      that is NOT `HUB_ADMIN_TOKEN`. `/app` is customer-reachable by design,
      so it belongs on your public ingress behind TLS — unlike `/admin`.
- [ ] Read [`DATA_RETENTION.md`](../DATA_RETENTION.md) — an org can delete
      its own trace or its entire account self-service
      (`delete_trace` / `request_account_deletion`), backed by an
      operator-CLI path (`purge-trace`/`purge-org`) for when it can't.
- [ ] If the CommonTrace Knowledge Base is not wanted for this deployment,
      do not call `commons-seed` or `approve-submission` — those are the
      only two things that ever put content into it (§13), so the corpus
      stays empty unless the operator acts. A pending `submit_kb_entry`
      proposal alone publishes nothing. For a stronger guarantee that the
      surface is gone entirely rather than merely empty, set
      `HUB_COMMONS_ENABLED=false`. `commons_overlap` against an empty
      Knowledge Base returns 0% with a note saying why, not an error.
- [ ] Decide whether to run a randomized holdout
      (`python -m hub.manage start-experiment <org_id> <rate>`), and decide
      it deliberately rather than by default — nothing turns it on, and the
      withheld fraction gets a worse product on purpose. It is also the
      only thing here that can answer "is this working?" causally rather
      than observationally, and STRATEGY.md §13.2 calls it the cheapest
      falsifier available. See `hub/README.md`, "The randomized holdout".
- [ ] Put `python -m hub.manage outcomes` on a recurring schedule alongside
      `usage`/`revenue`. Those report consumption, which is a lagging
      indicator that looks healthy right until a renewal a customer
      declines; `outcomes` reports whether each fleet's own numbers are
      actually moving, which is the leading one. Read its caveat before
      quoting anything from it to anyone: it is a before/after comparison,
      never a causal claim. See `hub/README.md`, "Fleet outcomes".
- [ ] If you *are* running the Knowledge Base, put
      `python -m hub.manage kb-review` on a recurring schedule — weekly is
      a reasonable start. It is the only thing that surfaces an entry a
      customer has flagged as a security concern, and an entry the field
      has voted down is served (ranked last, labelled `disputed`) until a
      human withdraws it. Nothing about the standing model retracts
      content on its own, deliberately; the queue is where that decision
      gets made. See `hub/README.md`, "Entry standing".

## 11. Known limitations (deliberate, documented)

| Limitation | Where |
|---|---|
| Rate limiting is per-process | §6, `hub/abuse.py` |
| Auth is API-key-only; no OAuth/JWT, no per-key scopes | `hub/README.md` |
| No self-service withdrawal of a pending Knowledge Base submission before an operator decides it | `DATA_RETENTION.md` §5 |
| No in-place edit of a Knowledge Base entry — the workflow is `kb-retract` then re-seed, which changes the trace id and resets its hit history | `DATA_RETENTION.md` §5 |
| Acting on a disputed or security-flagged entry needs an operator running `kb-review`; nothing withdraws content automatically | §10, `hub/README.md` |
| `fleet_outcomes` is observational (a before/after window), not a randomized experiment — it cannot separate this product's effect from anything else that changed | `hub/outcomes.py`, `commontrace/experiment.py` |
| A holdout's assignments depend on the org's `holdout_salt`; restarting an experiment starts a new one and earlier observations are no longer pooled | `hub/models.py:Organization.holdout_salt` |
| The CommonTrace Knowledge Base is lexical-match only; recall against paraphrased failures is ~11% (floor, not estimate) | `commons/eval/RESULTS.md` |
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
calling org, **and deletes them again before it exits** — so wiring this
into a deploy gate does not slowly fill a real org's corpus with the
check's own residue (which is not quarantined, and so would come back in
real agent searches and count against the org's plan storage). Pass
`--keep` to leave them for inspection; if cleanup fails for any reason the
run says so loudly and prints the `purge-trace` command.

Running it against production is safe; running it against a fresh
deployment before you hand out the first customer key is the point. It
exits non-zero on any failure, so `python -m hub.smoke ... && <promote>`
works as a gate.

### What a failure means

| Message | Cause |
|---|---|
| `could not reach <url>` | DNS, ingress, or the service is down. Check `/readyz` directly. |
| `HTTP 404 at <url>` | Wrong endpoint path. It defaults to `/mcp` (`HUB_STREAMABLE_HTTP_PATH`). |
| `rejected the API key (HTTP 401)` | Key is wrong, revoked, or expired. |
| `HTTP 5xx` | Reachable but failing. Check the container logs and `/readyz`. |
| `is rate limiting this client (HTTP 429)` | The read limiter refused it. The CLI paces itself and retries; a run that still fails means `HUB_READ_RATE_LIMIT_PER_MINUTE` is too low for this fleet. Check `commontrace_hub_rate_limited_total{limiter="http"}`. |
| `refused this write for rate limiting` | The per-org write limiter (`HUB_RATE_LIMIT_PER_MINUTE`, 120/min). Expected mid-way through a large first import; a run that fails at it needs a higher limit. Check `commontrace_hub_rate_limited_total{limiter="write"}`. |
| A named `[FAIL]` check | The server is up but a behavioural guarantee broke. Do not hand out keys. |

## 13. Running this for a single organization, privately, on your own fleet

Everything above works unchanged for one org running its own Hub for its
own fleet — that is the simplest deployment shape this server supports,
not a stripped-down mode. Tenant isolation, migrations, health probes,
backups, and the security checklist are identical whether one org uses
the Hub or a thousand do. Two things are specific to running it alone:

- **The CommonTrace Knowledge Base defaults to empty, and can be removed
  outright.** Left at its default, nothing is in it unless the operator
  runs `commons-seed` — there is no customer-facing tool that can put
  anything there, so `commons_overlap`/`commons_search` on an empty
  Knowledge Base cost nothing and return 0%/no candidates with a note
  explaining why, never an error. That is "nobody populated it." If the
  requirement is stronger than that — an internal-only deployment that
  must not consult anything beyond what the fleet itself captured, full
  stop — set `HUB_COMMONS_ENABLED=false`. `commons_overlap` and
  `commons_search` are then absent from the MCP tool surface entirely (an
  unknown-tool error to any client that tries), not merely empty;
  `python -m hub.smoke`'s tool-surface check reflects whichever mode the
  server is actually running in. Skip `commons-seed` either way if there
  is only ever going to be one org running this fleet's own on-prem
  memory — the Knowledge Base is optional substrate knowledge, not
  something a single-org deployment needs.
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
