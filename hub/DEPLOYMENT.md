# Deploying the CommonTrace Hub

What an operator needs to run the Hub for real, as opposed to the
local-checkout instructions in [`hub/README.md`](README.md).

> **Verification status, stated up front.** The application is exercised
> against a real PostgreSQL 16 instance by `hub/tests/` (1,051 tests, tenant
> isolation among them) on Python 3.10/3.11/3.12, alongside 1,334 client-side
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
> | Migrations onto an empty database, then `alembic check` | 17 revisions applied, no drift |
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
| **Two database roles** | One owner for migrations, one non-superuser runtime role for serving. See §2.1 — the Hub refuses to start if it finds itself serving as a role that silently bypasses the tenant-isolation policies. |

No Redis, no message broker, no object storage. State lives entirely in
Postgres.

### 2.1 Two database roles, and why the Hub refuses to start without them

Postgres skips **every** row-level-security policy for a superuser or a role
holding `BYPASSRLS` — silently. No error, no warning, no log line. The
policies still exist, `pg_policies` still lists them, an audit still finds
them, and they do nothing.

That is worse than not having RLS at all, because it is a guarantee an
operator believes in and does not have. It is also not hypothetical: the
official Postgres image makes `POSTGRES_USER` the cluster superuser, and
this repo's own `docker-compose.yml` pointed `HUB_DATABASE_URL` at exactly
that role — so the shipped evaluation stack installed migration
`d5c8b3a91e77`'s tenant-isolation policies and bypassed all of them.

So the deployment has two roles with different jobs:

| Role | Used by | Rights |
|---|---|---|
| **owner** (`commontrace`) | `alembic upgrade head`, one-shot, never serving | owns the schema, full DDL |
| **runtime** (`commontrace_app`) | the Hub process, every request | `SELECT/INSERT/UPDATE/DELETE` only; `NOSUPERUSER`, `NOBYPASSRLS`, no `CREATE` on the schema |

`docker compose up` creates both: `hub/postgres-init/10-runtime-role.sql`
runs once at first initialisation, before any table exists, and uses
`ALTER DEFAULT PRIVILEGES` so every table alembic creates afterwards — and
every table a future migration adds — grants the runtime role its DML
rights automatically. There is nothing to keep in sync by hand.

**On a managed Postgres** (RDS/Cloud SQL/Neon), run the same statements once
as the owner; the script's own header carries them, including the one-off
`GRANT ... ON ALL TABLES` an existing database with tables already in it
needs. Then point `HUB_DATABASE_URL` at the runtime role and keep the
owner's credentials for migrations only.

`hub/db.py:check_row_level_security` verifies this at startup:

- **Policies exist and the role bypasses them** → the Hub **refuses to
  start**, naming both remedies. Set `HUB_ALLOW_RLS_BYPASS=true` to
  acknowledge a deployment that intends to serve as owner/superuser and
  accept that tenant isolation rests on `hub/crud.py`'s own `org_id`
  predicates alone.
- **`HUB_REQUIRE_RLS=true`** (opt-in) → additionally refuses unless the
  policies are affirmatively installed *and* enforced, so a database nobody
  migrated, or one somebody dropped the policies from, is refused too.
- **Undeterminable** (the database is unreachable at boot) → warns and
  continues, always. "Cannot determine" is not "determined to be unsafe",
  and a diagnostic that turns a transient blip into a crash-loop is worse
  than the thing it diagnoses — `/readyz` already reports the process
  unready in that case.

One consequence worth knowing: with `HUB_RATE_LIMIT_BACKEND=postgres`, the
`hub_rate_limit_buckets` table is created by the **owner** (the init script
does it), not by the app at runtime. A role with no `CREATE` on the schema
cannot run `CREATE TABLE IF NOT EXISTS` *even when the table already
exists* — Postgres checks the schema privilege before the existence check —
so on a hand-built database, create that table as the owner too.

## 2. Configuration

Every setting is an env var, all documented in
[`hub/.env.example`](.env.example). The only one with no default is
`HUB_DATABASE_URL` — the server refuses to start without it rather than
guessing a connection string.

Sizing note: `HUB_DB_POOL_SIZE` (default 10) is **per replica**. The product
`HUB_DB_POOL_SIZE × replicas` must stay comfortably under your Postgres
`max_connections`, or a rolling deploy will exhaust connections while old
and new replicas overlap.

### Secrets from a real secret store (`hub/secrets_provider.py`)

Every genuinely secret setting (`HUB_DATABASE_URL`, `HUB_API_KEY_PEPPER`,
`HUB_ADMIN_TOKEN`, `HUB_CONSOLE_SECRET`, the Stripe keys,
`HUB_LEDGER_SIGNING_KEY`, `HUB_ENCRYPTION_KEY`/`_PREVIOUS` — marked
`(secret)` in `hub/.env.example`) can instead be supplied by pointing a
`{VAR}_FILE` variable at a file holding the value:

```bash
HUB_DATABASE_URL_FILE=/run/secrets/db_url
```

The file variant wins if the plain variable is also set, so a deployment
that wired up a real secret store never silently falls back to a stale
plaintext value left over from an earlier configuration.

This is deliberately a file convention, not one vendor's SDK — see
`hub/secrets_provider.py`'s module docstring for the full reasoning. It
means every one of these already works with no further code:

- A Vault Agent (or External Secrets Operator) sidecar writing to a shared volume.
- Any cloud provider's Kubernetes Secrets Store CSI driver (AWS, GCP, Azure).
- A plain Kubernetes `Secret` volume mount — `deploy/k8s/deployment.yaml`
  currently uses `envFrom` instead, which is simpler for a first
  deployment; switch to a mounted volume plus `_FILE` variables to keep
  secret values out of `kubectl describe pod`/the container's own
  environment listing.
- Docker/Swarm secrets, which are always files under `/run/secrets/`.

Operational settings (`HUB_HOST`, rate limits, `HUB_OIDC_ISSUER`, ...) are
not secrets and are read directly — the `_FILE` convention only applies to
the fields listed above.

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

### Sizing an experiment before you start it

`python -m hub.manage plan-experiment <org_id>` answers the question an
operator otherwise has to guess at: **what holdout rate can this org's own
volume actually answer with?** It reads that org's retrieval volume and its
own resolution rate, and says what rate a 10-point effect needs — or says
plainly that no rate can answer it in this window, which is the most useful
thing it can tell you and is worth knowing before the month rather than
after.

The arithmetic nobody does in their head: at a 10% holdout only one occasion
in ten lands in the control arm, so a run reaches an answer roughly **ten
times slower** than its occasion count suggests.

`start-experiment` also warns when the rate you chose cannot answer anything
at that org's observed volume. It still starts — your decision stands — but
it is said at the only moment the rate can be changed for free.

### Pre-registration, and handing over the rows

`start-experiment <org_id> [rate] [outcome] [notes]` **pre-registers** the
run: the primary outcome, the smallest effect worth acting on, the holdout
rate, the planned size, and the stopping rule, fingerprinted and stored
against the salt it was minted with. `causal_effects` and `value_delivered`
then diff the run against it and report every difference — a moved endpoint,
a changed detectable effect, a different randomization, a registration
written after the data started arriving. An experiment with no registration
is reported as unregistered rather than passing silently: settling what a
run measured once the results are visible is not a test of a hypothesis, and
the absence is itself the finding.

```bash
python -m hub.manage start-experiment <org_id> 0.2 resolved "Q1 support pilot"
python -m hub.manage export-assignments <org_id> assignments.csv
```

`export-assignments` writes **every arm decision** — including the occasions
that were assigned an arm and never reported, because those are the
attrition question and an export without them hands over a record with the
evidence already removed. It prints a digest over the canonical sorted rows,
and that digest is inside what `HUB_LEDGER_SIGNING_KEY` signs. So a customer
holding an invoice, an export and a signature can establish that all three
describe the same experiment — which is the difference between "our system
says you owe us this" and a number they can re-derive and disagree with.

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

- **It is read-only over this Hub's own data.** Nothing a browser does here
  writes to `Organization`, `Trace`, or any other row directly, which is why
  it carries no CSRF token — its session cookie is `SameSite=Strict`, so a
  forged cross-site request arrives with no session and is turned back at
  sign-in. Everything a customer can change in their own trace store still
  goes through MCP or the CLI, where it is authenticated and audited. The
  one exception carries its own trust boundary rather than weakening this
  one: with Stripe configured (below), an "Upgrade" click sends the browser
  to a Stripe-hosted page, and `Organization.plan` only ever changes later,
  from Stripe's own signed webhook call — never from the browser request.
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

### Self-serve signup

Set `HUB_SIGNUP_ENABLED=true` and the Hub also serves a public,
unauthenticated `POST /signup`: a visitor creates their own free-plan org
and first API key with no operator involved (unset — the default — means
the route does not exist, same posture as `/admin` and `/app`).

```bash
HUB_SIGNUP_ENABLED=true
```

Two things to know before enabling it on a public ingress:

- **No email verification.** This Hub has no outbound email integration to
  build one on. The blast radius of an uncontactable or fraudulent signup
  is bounded by the free plan's own limits (`hub/plans.py`) either way —
  the same ceiling every evaluator gets, verified or not.
- **No CAPTCHA.** The only abuse controls are a tight per-address rate
  limit and a honeypot field (`hub/signup.py`). That is enough for an
  unauthenticated route that mints a usable credential to not be a fully
  open oracle, but it is not CAPTCHA-strength — for a public-facing
  deployment expecting real traffic, put it behind whatever bot mitigation
  (a WAF, a CAPTCHA) you already run in front of other public signup forms,
  the same way you would for any other account-creation endpoint.

### Self-serve billing

Set `HUB_STRIPE_SECRET_KEY`, `HUB_STRIPE_WEBHOOK_SECRET`, and at least one
of `HUB_STRIPE_PRICE_TEAM`/`HUB_STRIPE_PRICE_SCALE` and a signed-in customer
can upgrade themselves via Stripe Checkout; `Organization.plan` then stays
in sync with what Stripe actually charged via `POST /billing/webhook` (also
unregistered until the webhook secret is set).

**All three or none.** `StripeSettings.checkout_configured` (`hub/billing.py`)
requires the key, the webhook secret, and a price together — not just enough
to sell an upgrade. The reason is specific: a deployment with a working key
and price but no webhook secret would show a working "Upgrade" button, take
a customer's real payment, and then have no route left to ever learn it
happened, so the plan never moves off `free` — charged and never upgraded,
silently. The same requirement gates Stripe's Billing Portal (where an
already-subscribed customer manages or cancels), for the same reason in the
other direction: a cancellation made there is also delivered only through
the webhook, and without it a canceled customer keeps their paid entitlement
indefinitely.

```bash
HUB_STRIPE_SECRET_KEY=sk_live_...
HUB_STRIPE_WEBHOOK_SECRET=whsec_...      # from the endpoint you register in
                                          # the Stripe dashboard, pointed at
                                          # https://<this-hub>/billing/webhook
HUB_STRIPE_PRICE_TEAM=price_...
HUB_STRIPE_PRICE_SCALE=price_...
```

No Stripe SDK — `hub/billing.py` calls Stripe's REST API directly over
`httpx` (already a Hub dependency) and verifies webhook signatures with one
documented HMAC check, rather than adding a second pinned dependency for a
handful of calls to one vendor.

### Signing the value ledger

`value_delivered`'s `ledger` (`commontrace/value.py`) is hash-chained: every
line carries a SHA-256 over itself and the previous line's hash, so editing
a figure, dropping the memory that measured as HURTING, or reordering to
bury it all break the chain, and `commontrace.value.verify_ledger` proves
it. That chain's genesis and algorithm are both public by design — the
whole point is that a customer's finance team can reimplement the check
independently — which means it only proves the ledger is *internally
consistent*, not *who issued it*. Anyone with write access to wherever a
ledger ends up stored (a compromised account, a malicious insider, an
issuer understating its own invoice after the fact) could fabricate an
entire replacement chain from different figures, and it would verify
exactly as cleanly as the real one.

Set `HUB_LEDGER_SIGNING_KEY` to close that gap. Every `value_delivered`
response is then also signed with HMAC-SHA256
(`commontrace.value.sign_ledger`) over the chain's root, bound to the org
and the timestamp it was issued at, and returned as `signature` +
`issued_at` alongside the ledger. A customer verifies it with
`commontrace.value.verify_ledger_signature` against the same key — so a
signature only validates for a ledger this deployment actually issued, not
merely one that follows the public rules. Leave it unset and
`value_delivered` still returns the hash-chained ledger, but `signature` is
`null` and `signature_reason` says explicitly that this deployment has not
opted into issuer authentication, rather than silently looking more audited
than it is.

```bash
HUB_LEDGER_SIGNING_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
```

**This key now has a second job, and rotating it breaks both.** Webhook
signing secrets are *derived* from it rather than stored
(`hub/events.py:derive_secret`), which is what keeps a database dump from
yielding the ability to forge an event. The consequence an operator has to
know before rotating: changing `HUB_LEDGER_SIGNING_KEY` silently changes
every endpoint's signing secret, so every customer's webhook receiver starts
rejecting deliveries as unsigned, and every previously issued ledger
signature stops verifying. Neither failure announces itself at rotation
time — the receiver just starts returning 401 and the queue starts
retrying. If you must rotate it, re-issue every endpoint's secret
(`webhook-rotate`) and tell every customer holding one, in the same
maintenance window.

Set it **identically across every replica**. Unlike `HUB_API_KEY_PEPPER`
(which tolerates a per-process fallback, because a missing pepper only ever
weakens one timing defense), a value ledger is meant to be verified by the
customer *later*, against whichever replica happened to sign it at request
time — a key that silently varied by process or by restart would make some
invoices verify and others not, for no reason visible to the customer
holding them. There is no key versioning here: rotating this value
invalidates verification of every already-issued invoice unless you keep
the retired key available out-of-band, specifically to still check
signatures minted under it.

### Encryption at rest

This has two different answers depending on which column you mean, and
conflating them is the mistake to avoid.

**Trace content (`title`, `context_text`, `solution_text`, `subject_ids`)
is NOT encrypted at the application layer, deliberately.**
`hub/models.py`'s `Trace.search_vector` is a Postgres `GENERATED STORED`
column computed directly, in SQL, from `title`/`context_text`/
`solution_text` — encrypting them here would mean Postgres builds that
tsvector from ciphertext, so `search_traces` would keep running without
error while silently never matching anything again. `subject_ids` has the
same conflict one level down: `find_traces_by_subject`/
`purge_traces_by_subject` (the subject-erasure path `DATA_RETENTION.md`
documents) depend on exact array-membership matches against a GIN index,
which a fresh-nonce-per-value scheme (the only kind worth using) makes
impossible — the same id would encrypt to different ciphertext every time
it's written.

If your compliance program requires encryption at rest for this content —
most do, and this is the normal, correct way to satisfy that requirement
for a full-text-search-heavy schema — configure it at the storage layer
**underneath** Postgres instead, where the database itself still operates
on plaintext internally and neither of the above breaks:

- A managed provider's disk/volume encryption (RDS, Cloud SQL, and
  equivalents all support this, usually on by default for a new instance).
- An encrypted filesystem under a self-hosted Postgres data directory
  (LUKS or equivalent).
- A Postgres Transparent Data Encryption extension, if your distribution
  ships one.

None of these are something this codebase can configure on your behalf —
they're a property of where and how you run Postgres — which is also why
`SOC2_READINESS.md`'s Confidentiality table lists this as an operator
responsibility rather than a control this code implements.

**`WebhookEndpoint.url` (`hub/encryption.py`) is the one column this code
*does* encrypt, opt-in.** A webhook URL is never searched or matched by
Postgres, so none of the above conflict applies, and it sometimes carries
a bearer token or shared secret in its path or query string — exactly the
kind of value that should not sit in plaintext in a `pg_dump`. Set
`HUB_ENCRYPTION_KEY` to turn it on:

```bash
python -m hub.manage generate-encryption-key
```

Unset (the default) means this column is stored as plaintext, exactly as
it always was — every existing deployment is unaffected until it opts in.
Rotating: move the current value into `HUB_ENCRYPTION_KEY_PREVIOUS` before
replacing `HUB_ENCRYPTION_KEY`, so endpoints registered under the old key
keep decrypting until they're next re-registered or `webhook-rotate`d.

### Metrics

`GET /metrics` serves Prometheus text format:

| Metric | Labels | What it answers |
|---|---|---|
| `commontrace_hub_requests_total` (counter) | `method`, `path`, `status` | Traffic and error rate per route. |
| `commontrace_hub_request_duration_ms` (histogram: `_bucket{le}`, `_sum`, `_count`) | `path` | Latency **distribution** per route -- feed it to PromQL's `histogram_quantile()` for p50/p95/p99, not just a mean. Bucket boundaries are `Metrics.BUCKETS_MS` in hub/observability.py. |
| `commontrace_hub_rate_limited_total` (counter) | `limiter` (`http`, `write`) | **How often you are refusing customers, and by which limiter.** |

Example p99 query for the `/mcp` route:

```promql
histogram_quantile(0.99,
  sum(rate(commontrace_hub_request_duration_ms_bucket{path="/mcp"}[5m])) by (le)
)
```

The rate-limited counter is the one to alert on unconditionally. Rate limiting is otherwise invisible
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
public internet, the same as any `/metrics`. Where it cannot be kept off a
reachable network, set `HUB_METRICS_TOKEN`: a scrape must then send
`Authorization: Bearer <token>` (Prometheus: an `authorization:` block with
`credentials_file:`), and anything else gets a 401.

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

**Kubernetes:** `deploy/k8s/` has a reference manifest set (ConfigMap,
Secret shape, a migration Job, Deployment, Service, HPA, PDB) for a
platform where Compose isn't the deployment target — see that directory's
own README for what's included, what's deliberately not (no Postgres
manifest, no Ingress), and what is and isn't rehearsed in CI.

**Without containers:**

```bash
pip install -r hub/requirements.txt
export HUB_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/commontrace_hub
python -m alembic -c hub/alembic.ini upgrade head
python -m hub.main
```

On Python 3.12 (what the Docker image ships), add
`-c hub/requirements-lock.txt` to that `pip install` for the same exact,
tested set of transitive dependencies the image gets — see that file's own
header for why it's scoped to one interpreter version rather than every
Python this Hub supports.

Clients connect to `https://<your-host>/mcp` with
`Authorization: Bearer <api-key>`.

## 6. Scaling, and rate-limit backends

The Hub is stateless apart from Postgres, so replicas scale out normally —
**with one caveat that depends on a setting**:

> By default, rate limiting (`hub/abuse.py`) is an **in-process token
> bucket**. It resets on restart and does not coordinate across replicas, so
> N replicas allow roughly N× the configured rate.

`HUB_RATE_LIMIT_BACKEND` selects which `RateLimiter` implementation backs
every limiter the Hub constructs (`contribute_trace`/`amend_trace`, the
per-request read limiter, and the pre-auth attempt limiter alike):

- `memory` (default) — the in-process bucket above. Exact current behavior;
  an existing deployment that never sets this is unaffected. Right choice
  for a single replica, or when you'd rather cap the *effective* rate by
  setting `HUB_RATE_LIMIT_PER_MINUTE` to `desired ÷ replicas`, or enforce
  the real limit at your ingress instead.
- `postgres` — a token bucket backed by a `hub_rate_limit_buckets` table in
  this same database (created with `CREATE TABLE IF NOT EXISTS` on first
  use, not an Alembic migration — no schema change to run). Every replica
  sharing one `HUB_DATABASE_URL` enforces one real shared limit instead of
  its own independent N-way allowance. No new infrastructure dependency
  (no Redis), but the every-request call sites this gates
  (`ApiKeyAuthMiddleware`, `contribute_trace`/`amend_trace`) call
  `RateLimiter.allow()` synchronously — a constraint of the existing
  interface, unchanged by this feature — so each `allow()` call becomes a
  **blocking** Postgres round trip that stalls that replica's entire event
  loop (every other in-flight request on it, not just the one calling
  `allow()`) for its duration, typically sub-millisecond to a few ms
  against a co-located Postgres but real under load or DB latency spikes.
  It also adds query load to the same database serving every other
  request. Budget for both before enabling this on a deployment with a
  strict latency target; a lightly-loaded internal deployment behind a
  handful of replicas is the comfortable case. One exception either way:
  the `/readyz` liveness-probe limiter is constructed directly in
  `hub/server.py`, outside this setting, and always stays in-process (a
  per-replica liveness check has no reason to be shared).

Set `HUB_RATE_LIMIT_BACKEND=postgres` once you're running more than one
replica and need the configured rate to actually mean what it says.

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

### 9b. RPO/RTO: what this drill measures, and what it deliberately does not

Audit §7.5 named "no RPO/RTO, restore or deletion drills" as missing.
The drill above is not hypothetical — it was run end to end (dump,
restore into a fresh database, `alembic check`, and the full `hub.smoke`
suite including tenant isolation) against a seeded database of 8,000
traces across two orgs (~28 MB). Measured on that run, on this sandbox's
hardware:

| Step | Measured time |
|---|---|
| `pg_dump` (custom format) | 0.29s |
| `pg_restore` into a fresh database | 2.78s |
| `alembic check` (schema-currency verification) | 1.33s |
| `hub.smoke` full suite (both orgs, tenant isolation included) | 4.78s |

**What this does and does not establish, stated precisely so neither
number is mistaken for a commitment:**

- This is the *mechanism's* time cost — dump, restore, and verify against
  a database of this specific size — not a promised production RTO. A
  production database with more data restores slower; `pg_dump`/
  `pg_restore` scale with data volume, so re-run this drill against a
  copy of your actual production size to get a number that means
  something for your deployment, not this test dataset's.
- **RPO (how much data a restore could lose) is entirely a function of
  backup *frequency*, which is an operator/hosting decision this
  document cannot make.** A managed Postgres provider's continuous
  WAL archiving can put RPO in the seconds; a nightly `pg_dump` cron
  puts it at up to 24 hours. Pick a frequency, then your RPO is that
  frequency's own interval — no code change alters this.
- **Production RTO also includes time this local drill has none of**:
  noticing the outage, deciding to restore, provisioning a database to
  restore into, and DNS/traffic cutover. The table above is the
  restore-and-verify slice alone — the part that is actually testable
  independent of a specific production topology.
- This drill is a rehearsal you can re-run, not a standing commitment.
  Nothing here schedules it, alerts if it has gone stale, or promises a
  cadence — that is the "staffed rota" half of §7.4/§7.6, which remains
  a real operator/business decision, not a repository file.

## 9a. Break-glass: every admin account is disabled or its IdP is unreachable

Human sign-in (`hub/README.md` "Human users, roles, and OIDC SSO") has no
password and no recovery email — a `User` row authenticates only through
its linked OIDC identity, and there is no self-service anything. That is
the right default (no secondary credential to leak, no password reset flow
to phish), and it means the ordinary path to a `ROLE_SECURITY_ADMIN`/
`ROLE_OWNER` account has exactly one dependency: the identity provider.
This is what to do when that dependency fails — every such account is
disabled, or the IdP itself is down/misconfigured, and nobody can sign in.

**The recovery path is always the same one an operator already has**:
direct database access, via `hub.manage`, run from wherever
`HUB_DATABASE_URL` is reachable (a bastion host, a deploy box, `kubectl
exec` into the Hub's own pod — whatever your topology already trusts with
that connection string; this is not a new credential, it is the same one
that runs every migration).

```bash
# 1. Confirm what's actually broken before changing anything.
python -m hub.manage list-users <org_id>          # who exists, whose role, who is disabled

# 2a. An account is disabled that should not be -- re-enable it.
python -m hub.manage enable-user <user_id>

# 2b. No working Security Admin/Owner exists at all -- mint a fresh one.
#     This does NOT need the IdP to be reachable: create-user only writes
#     a row, and role alone does not authenticate anybody.
python -m hub.manage create-user <org_id> <email> owner

# 3. The IdP is unreachable, but you need this person signed in NOW --
#    link a DIFFERENT, reachable IdP's identity to the row instead of
#    waiting for the original one to come back. (Standing configuration
#    change: point HUB_OIDC_ISSUER/HUB_OIDC_JWKS_URI at the new IdP.)
python -m hub.manage link-sso <user_id> <new_issuer> <new_external_subject>

# 4. Verify: the recovered account can actually reach a tool, not just
#    that the row looks right.
python -m hub.manage audit-log <org_id> | head    # confirm what you just did, and by whom
```

**Every one of these steps is an audited action** (`hub/manage.py`'s own
`audit.record` call on `create-user`/`enable-user`/`link-sso`), so a
break-glass recovery leaves the same trail an ordinary one would — there
is no "off the books" path here, only a faster one that does not depend on
the thing that just broke.

**An automatic alert fires when it is used.** Steps 2a/2b above
(`enable-user`, `create-user`) queue a `user.privileged_role_granted`
webhook event (`hub/events.py`) the instant they leave anyone holding
`ROLE_SECURITY_ADMIN`/`ROLE_OWNER`, delivered through whatever endpoint an
org has already subscribed (`webhook-add`). It fires identically for
routine admin onboarding and for this exact recovery flow — nothing in the
data model distinguishes the two — so subscribing to it is what gives an
org the "someone just got Owner" signal this procedure alone cannot: the
procedure produces an audit-log row after the fact, the event pushes to
whoever is watching in real time.

**Still a documented procedure, not fully built tooling.** There is no
time-boxed emergency token and no requirement for a second person to
witness the recovery, beyond what `hub.manage`, `audit-log`, and the alert
above already give you. For a deployment that needs stronger guarantees
than "whoever can reach `HUB_DATABASE_URL` can do this," that is the next
thing to build, and it is listed as not done in `AUDIT_RESPONSE.md` §1.2
rather than implied by this section existing.

## 10. Security checklist before a client's data lands

- [ ] TLS terminated in front of the Hub (API keys are bearer credentials).
- [ ] `HUB_DATABASE_URL` from a secret store, not a file in the repo —
      `HUB_DATABASE_URL_FILE` (§2, "Secrets from a real secret store")
      lets your platform's actual secret manager supply it with no
      plaintext env var anywhere.
- [ ] `HUB_DATABASE_URL` points at the **runtime** role, not the owner — a
      `NOSUPERUSER`/`NOBYPASSRLS` role with DML grants only, so the
      tenant-isolation policies actually apply (§2.1). The Hub refuses to
      start otherwise; if you had to set `HUB_ALLOW_RLS_BYPASS=true` to get
      it up, that is a finding, not a fix.
- [ ] Postgres not publicly reachable; Hub reaches it over a private network.
- [ ] API keys issued with an expiry (`issue-key <org_id> <days>`) rather
      than never expiring.
- [ ] API keys issued with the **narrowest scope** that does the job
      (`issue-key <org_id> 90 read,write` for a production agent, `read`
      for a dashboard). Omitting scopes grants `read,write,admin`, which
      means that credential can also delete the organization. See
      `hub/scopes.py`.
- [ ] Rate limiting understood per §6 (or enforced at the ingress).
- [ ] Backups on, and a restore actually rehearsed.
- [ ] `HUB_ADMIN_TOKEN` either unset, or set to a real secret with `/admin`
      reachable only from your operator network.
- [ ] `HUB_OPERATOR_ORG_ID` set to your own org if you intend to accept
      Knowledge Base proposals from the console (it fails closed otherwise).
- [ ] `HUB_CONSOLE_SECRET` either unset, or set to a fresh random secret
      that is NOT `HUB_ADMIN_TOKEN`. `/app` is customer-reachable by design,
      so it belongs on your public ingress behind TLS — unlike `/admin`.
- [ ] If `HUB_SIGNUP_ENABLED=true`, put whatever bot mitigation (WAF,
      CAPTCHA) you already run in front of other public signup forms in
      front of `/signup` too — its own abuse controls are a rate limit and
      a honeypot, not CAPTCHA-strength. See §4, "Self-serve signup".
- [ ] `HUB_STRIPE_SECRET_KEY`/`HUB_STRIPE_WEBHOOK_SECRET` from a secret
      store, same as `HUB_DATABASE_URL`. Set all four Stripe variables
      together or none — `checkout_configured` refuses to offer Checkout
      or the Billing Portal on a partial configuration, because either one
      without a registered webhook silently desyncs `Organization.plan`
      from what Stripe actually charged. See §4, "Self-serve billing".
- [ ] `HUB_LEDGER_SIGNING_KEY` set (from a secret store, identical across
      every replica) if any customer is billed off `value_delivered` —
      otherwise its ledger is only hash-chained, not signed by the issuer,
      and `signature` in the response is `null`. See §4, "Signing the value
      ledger".
- [ ] Encryption at rest for Trace content is configured at the storage
      layer (managed-provider disk encryption, LUKS, or a Postgres TDE
      extension) if your compliance program requires it — this code
      deliberately does not encrypt `title`/`context_text`/`solution_text`/
      `subject_ids` itself, because those columns are what Postgres
      full-text-searches and exact-matches over. See §4, "Encryption at
      rest". `HUB_ENCRYPTION_KEY` (same section) covers a different,
      narrower column (`WebhookEndpoint.url`) and does not substitute for
      this.
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
| No *browser* login: a person authenticates with a bearer JWT their IdP already issued (`hub/sso.py` verifies it), and the console signs in with an API key — there is no OAuth2 Authorization Code/PKCE redirect flow and no SAML, so nothing here can start a login from a browser on its own | `hub/sso.py`, `hub/console.py`, `AUDIT_RESPONSE.md` §1.2 |
| No self-service withdrawal of a pending Knowledge Base submission before an operator decides it | `DATA_RETENTION.md` §5 |
| No in-place edit of a Knowledge Base entry — the workflow is `kb-retract` then re-seed, which changes the trace id and resets its hit history | `DATA_RETENTION.md` §5 |
| Acting on a disputed or security-flagged entry needs an operator running `kb-review`; nothing withdraws content automatically | §10, `hub/README.md` |
| `fleet_outcomes` is observational (a before/after window), not a randomized experiment — it cannot separate this product's effect from anything else that changed | `hub/outcomes.py`, `commontrace/experiment.py` |
| Causal verdicts are read from a *running* experiment, so they use an anytime-valid boundary — trustworthy under continuous peeking, but slower to establish a small effect than a fixed threshold would be (measured: 95%→69% power at a +10pp effect within 1,000 occasions, against a false-positive rate of 28%→1.3%) | `commontrace/experiment.py:analyze` |
| Per-trace value contributions cannot be summed when traces share occasions, which is the normal case here — the policy-level comparison is reported instead | `commontrace/value.py` |
| A holdout's assignments depend on the org's `holdout_salt`; restarting an experiment starts a new one and earlier observations are no longer pooled | `hub/models.py:Organization.holdout_salt` |
| The CommonTrace Knowledge Base is lexical-match only; recall against paraphrased failures is ~11% (floor, not estimate) | `commons/eval/RESULTS.md` |
| `CO_RETRIEVED` trace relations not computed | `hub/README.md` |
| Self-serve billing covers Checkout + the Billing Portal only — no dunning, tax handling, or invoicing UI beyond what Stripe's own hosted pages provide | §4, `hub/billing.py` |
| Self-serve signup has no email verification and no CAPTCHA (a rate limit + honeypot only) | §4, `hub/signup.py` |
| No production-like rehearsal (TLS, managed PG, multi-replica) | top of this file |
| `deploy/k8s/` manifests are reviewed, not applied against a real cluster in CI | `deploy/k8s/README.md` |
| Trace content (`title`/`context_text`/`solution_text`/`subject_ids`) is not encrypted at the application layer — full-text search and exact-match array queries depend on those columns being computable by Postgres itself; use storage-layer encryption instead | §4 "Encryption at rest", `hub/encryption.py` |

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
