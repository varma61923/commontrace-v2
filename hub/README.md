# CommonTrace Hub

The server side of the "Hub" conformance tier in
[`protocol/PROTOCOL.md`](../protocol/PROTOCOL.md#5-store-two-conformance-tiers).

Until this was built, `commontrace/commands/sync_cmd.py` only printed
instructions for a Hub that didn't exist anywhere in this repository or any
deployed service (see the repo's Phase 0 resolution: **(B) no Hub server
exists**). This directory is that server, plus the client wiring in
[`commontrace/hub_client.py`](../commontrace/hub_client.py) that makes
`commontrace sync` actually work end-to-end.

## What it is

An MCP server over streamable-HTTP. Six tools match `PROTOCOL.md` §5's
names and semantics exactly:

`search_traces(query, tags)` · `contribute_trace(title, context_text, solution_text, tags, agent_type)` · `get_trace(id)` · `vote_trace(id, vote, feedback_tag, feedback_text)` · `amend_trace(id, ...)` · `list_tags()`

Four more are Hub-specific and outside the protocol, all org-scoped like
the six: `share_trace(id)` · `unshare_trace(id)` · `commons_overlap(failures)`
for the opt-in cross-org commons, and `account_usage()` for the caller's own
plan and meter. `hub/smoke.py` pins the tool surface, so a tool appearing or
disappearing fails a post-deploy check rather than surprising a client.

The three commons tools can be removed from the surface entirely with
`HUB_COMMONS_ENABLED=false` (hub/config.py) -- an unknown-tool error to any
client that tries, not a per-call refusal, so "no cross-org sharing" holds
for a deployment even if every org on it forgets the feature exists. See
`hub/DEPLOYMENT.md` §13 for an internal-only deployment that wants this.

Any MCP-capable agent (Claude Code, Cursor, Devin, Windsurf, a generic MCP
client) can attach with a plain config block and an API key — no
CommonTrace-specific SDK, per `PROTOCOL.md` §8.

## Architecture at a glance

```
hub/config.py      env-driven settings, no unsafe defaults
hub/models.py      SQLAlchemy 2.0 ORM: Organization, ApiKey, Trace, Vote, TraceRelation, UsageCounter
hub/db.py          async engine/session plumbing
hub/schema_validation.py   loads protocol/schemas/*.json from disk, validates against them
hub/auth.py        argon2 API-key hashing/verification/rotation/expiry + request-scoped org_id
hub/abuse.py       size limits, per-org rate limiting, a spam heuristic -> quarantine
hub/audit.py       append-only audit-log writes (who did what, no secrets, no content)
hub/observability.py  JSON logging, request-id correlation, /healthz + /readyz
hub/plans.py       entitlements: what each plan grants, and the credit contributors earn
hub/crud.py        every tool's actual query logic -- ALWAYS org_id-scoped in SQL
hub/server.py      thin MCP wiring: auth middleware + tool handlers that call crud.py
hub/main.py        `python -m hub.main` -- run the server
hub/manage.py       `python -m hub.manage <cmd>` -- org/API-key operator CLI
hub/alembic/        migrations (see "Running locally" below)
hub/DEPLOYMENT.md   running it for real: probes, scaling, backups, security checklist
hub/tests/          pytest suite, including test_tenant_isolation.py
```

`hub/crud.py`'s module docstring explains the layering in detail. The short
version: `hub/server.py` never touches SQL, `hub/crud.py` never touches MCP
protocol shapes, and every read/write in `crud.py` that touches the `traces`
table puts `org_id` in the SQL `WHERE` clause itself — never "fetch rows,
then filter in Python." That's what makes tenant isolation a property of the
query layer, not an application-level convention someone can forget.

For a real deployment (containers, probes, scaling, backups, security
checklist) see **[hub/DEPLOYMENT.md](DEPLOYMENT.md)**. The rest of this
file covers running it from a checkout and the design decisions behind it.

## Running locally

Requires a real Postgres instance (this was developed and tested against a
local Postgres 16). For a containerized stack instead, see
[DEPLOYMENT.md](DEPLOYMENT.md).

```bash
pip install -r hub/requirements.txt

createdb commontrace_hub          # or your usual DB provisioning
cp hub/.env.example hub/.env      # fill in HUB_DATABASE_URL etc.
export $(grep -v '^#' hub/.env | xargs)   # or use your process manager

python -m alembic -c hub/alembic.ini upgrade head

python -m hub.manage create-org "Acme Corp"
python -m hub.manage issue-key <org_id_from_above> 90   # prints the raw key ONCE; expires in 90 days

python -m hub.main   # serves streamable-HTTP MCP on HUB_HOST:HUB_PORT/mcp
```

Point any MCP client at `http://<host>:<port>/mcp` with
`Authorization: Bearer <api-key>`. `GET /healthz` (liveness) and `GET /readyz`
(readiness, checks the database) are unauthenticated — see
[DEPLOYMENT.md §4](DEPLOYMENT.md#4-health-probes) for why they are separate
and which probe to attach to each.

### Running the tests

```bash
createdb commontrace_hub_test
export HUB_TEST_DATABASE_URL=postgresql+asyncpg://.../commontrace_hub_test
python -m pytest hub/tests/ -q
```

This is a separate invocation from the main `python -m pytest tests/ -q`
suite on purpose: the Hub's dependency footprint (SQLAlchemy, asyncpg,
Alembic, argon2-cffi, a running Postgres) is much heavier than the
PyYAML-only CLI core, and `tests/` should keep working with nothing but a
bare `pip install -e .`.

## Design decisions worth reading before you extend this

### Tenant isolation vs. the cross-org "commons" pitch

The brief this server was built from calls tenant isolation "the
highest-priority requirement" and specifies a hard test: as `org_a`, zero
rows belonging to `org_b` may ever appear in any tool's responses,
and `get_trace` on a known `org_b` id must 404, never 403 (never confirm the
id exists). `hub/tests/test_tenant_isolation.py` enforces exactly that.

That is in real tension with the product pitch elsewhere in this repo
("CommonTrace ... turning AI agent experience into validated, reusable
lessons ... across organizations"): a Hub where every org only ever sees its
own traces isn't a commons at all.

This implementation resolves that tension by getting the walls right first
and then opening exactly one door, deliberately.

**The walls.** Every one of the six original read paths —
`search_traces`, `get_trace`, `vote_trace`, `amend_trace`, `list_tags`,
`contribute_trace` — is still unconditionally scoped to the caller's own
`org_id`, and `hub/tests/test_tenant_isolation.py` passes unchanged.
Nothing about the commons loosened them.

**The door.** `Trace.shared_with_commons` is now live, and it is the only
field by which a row can cross an org boundary:

- It is `false` by default and is **never** set implicitly. An org opts in
  per trace via `share_trace`, which records a `shared_rationale` for audit
  and can only reach traces that org already owns.
- `unshare_trace` withdraws it, clearing the stored signature so it stops
  matching immediately.
- Quarantined traces cannot enter the commons — that would propagate
  exactly what quarantine exists to contain.
- The one query that reads across orgs, `commons_overlap`, requires
  `shared_with_commons AND NOT quarantined` and additionally excludes the
  caller's own rows.

The boundary being drawn is **substrate failures are shared; business logic
stays private** (`STRATEGY.md` §4). The Hub cannot judge which is which, so
that call is the contributing org's, made explicitly and recorded.

**Why it is signatures-in, consented-content-out.** The question a prospect
wants answered before contributing anything is "how many of the failures my
fleet keeps hitting has someone else already solved?" Answering it must not
require uploading those failures. So the client MinHashes locally and sends
only signatures; what comes back is drawn only from traces whose owners
explicitly shared them. Stated limitation, not glossed: MinHash is not a
cryptographic privacy guarantee — a party who can guess a candidate string
can test whether it is present. Private set intersection is the real fix and
is a named follow-up, not a quiet assumption.

One consequence worth knowing: because `vote_trace`/`amend_trace`/
`get_trace` remain scoped to the caller's own org, an org can only vote on
or amend its own traces. That is correct, not a bug — commons participation
grants visibility, not write access to someone else's history.

### Why there's no `lessons` table

`Trace` and `Lesson` are both loaded by `hub/schema_validation.py` (per the
brief: load both schema files from disk, don't hand-write validation logic),
but only `Trace` has a table in `hub/models.py`. None of the six Hub tools
accept or return a Lesson-shaped object — per `PROTOCOL.md` §4, "Lessons are
local-store scaffolding ... the Curator/Validator roles turn Traces into
Lessons before promoting the durable, reusable half of a Lesson to a Hub
Trace via `contribute_trace`." A `lessons` table would be dead schema. If a
future Hub tool ever needs to accept a Lesson, `validate_lesson()` is already
there and ready.

### Auth follow-ups (not implemented)

API-key-per-org (argon2-hashed, shown once, rotatable via
`hub/manage.py rotate-key`) is the whole auth story today, per the brief's
explicit MVP scope. Not implemented, and worth doing before this serves
traffic beyond a pilot:

- **OAuth/JWT.** The `mcp` SDK's built-in auth framework
  (`mcp.server.auth`) is OAuth-resource-server-shaped (issuer URLs, token
  introspection) and deliberately unused here — see `hub/auth.py`'s module
  docstring for why a small Starlette middleware was simpler and more
  honest about what's actually implemented than forcing API keys through
  an OAuth-shaped surface that isn't OAuth.
- **Per-key scopes.** Every key currently has full read/write access to its
  org's traces. Read-only keys, or per-tool scoping, aren't implemented.

### Abuse controls (implemented, with a known scaling limit)

`hub/abuse.py`'s rate limiter is an in-memory token bucket keyed by
`org_id`. That's fine for a single server process and is what the brief's
"start conservative" MVP scope calls for, but it resets on restart and does
not coordinate across multiple instances. A horizontally-scaled deployment
needs a shared store (Redis `INCR`+`EXPIRE`, or a Postgres-backed bucket
table) — not implemented, to avoid pulling in a Redis dependency for an MVP
that's meant to run as one process.

The spam heuristic (`suspicion_reason` in `hub/abuse.py`) is intentionally
simple — too many URLs, or near-zero character diversity — and is explicitly
documented in its own docstring as a placeholder, not a moderation system.
Replace/extend it as real abuse patterns are observed; do not read its
current thresholds as a considered content-moderation policy.

### `related` / `CO_RETRIEVED` — partially implemented

`Trace.related` (in `trace.schema.json`) documents three relationship kinds:
`SUPERSEDES`, `AMENDS`, `CO_RETRIEVED`. `amend_trace` populates `AMENDS`
(from the new trace) and `SUPERSEDED_BY` (on the original) via
`hub/models.py`'s `TraceRelation` table. `CO_RETRIEVED` (traces that tend to
surface together in the same `search_traces` call) is not computed — it
needs session-level co-retrieval tracking that wasn't in scope for this MVP.
`trust` (in `_to_wire`/`hub/crud.py:vote_trace`) is a simple
`up / (up + down)` ratio with a neutral `0.5` prior when there are no votes
yet; treat it as a starting point, not a calibrated reputation model.

### Container image: built and smoke-tested in CI

`Dockerfile` and `docker-compose.yml` exist at the repo root (an earlier
revision of this file said they deliberately did not — that is no longer
true). They were authored in an environment with no Docker daemon, so CI's
`docker-build` job is what actually proves them: it builds the image, starts
the container, and asserts `/healthz` serves while `/readyz` returns 503
with no database reachable — which also confirms the liveness/readiness
split behaves correctly in a real container, not just in unit tests.

Still unproven, so worth saying: **the compose stack is not exercised by
CI** (only the image is), and neither has been run against a
production-like environment. Do a rehearsal deploy first.

## Operator CLI (`hub/manage.py`)

There is no web admin panel — this CLI *is* the admin/monitoring surface,
consistent with the rest of the Hub being a from-a-checkout service you
operate, not a hosted product with its own UI. A web dashboard is a
reasonable future addition, but it needs its own cross-org admin auth model
(a superadmin credential, distinct from the org-scoped API keys `hub/auth.py`
issues) — a bigger, separate decision this pass didn't make.

```
python -m hub.manage create-org <name>
python -m hub.manage issue-key <org_id>
python -m hub.manage rotate-key <key_id>
python -m hub.manage revoke-key <key_id>
python -m hub.manage list-orgs

python -m hub.manage stats                       # orgs, active keys, traces, quarantined, votes, mean trust
python -m hub.manage list-quarantined [org_id]    # the abuse-control review queue (hub/abuse.py)
python -m hub.manage release-quarantine <trace_id>  # reviewed, it's fine -> becomes search_traces-eligible
python -m hub.manage purge-trace <trace_id>       # permanent delete, irreversible
python -m hub.manage purge-org <org_id>           # permanent delete (cascades), irreversible
```

The raw API key is only ever printed at issuance/rotation time — it is
hashed with argon2 before the row is written and never logged or returned by
any tool/endpoint afterward. There is no "show me the key again" path by
design; rotate if it's lost.

`purge-trace`/`purge-org` are the data-deletion path DATA_RETENTION.md
previously documented as entirely missing (only "a direct database
operation run by whoever operates Postgres" existed). They're still
operator/DB-access-trust-level only — deliberately not exposed as a
seventh MCP tool an org's own API key could call. Handing self-service
deletion to an org's own credential is a real feature with its own
authorization questions (should a single compromised key be able to wipe
an org's entire trace history with no confirmation step?) that this pass
didn't design; CLI-only for now is the conservative default, not a
permanent decision.
