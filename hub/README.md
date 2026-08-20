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

An MCP server exposing exactly six tools over streamable-HTTP, matching
`PROTOCOL.md` §5's names and semantics:

`search_traces(query, tags)` · `contribute_trace(title, context_text, solution_text, tags, agent_type)` · `get_trace(id)` · `vote_trace(id, vote, feedback_tag, feedback_text)` · `amend_trace(id, ...)` · `list_tags()`

Any MCP-capable agent (Claude Code, Cursor, Devin, Windsurf, a generic MCP
client) can attach with a plain config block and an API key — no
CommonTrace-specific SDK, per `PROTOCOL.md` §8.

## Architecture at a glance

```
hub/config.py      env-driven settings, no unsafe defaults
hub/models.py      SQLAlchemy 2.0 ORM: Organization, ApiKey, Trace, Vote, TraceRelation
hub/db.py          async engine/session plumbing
hub/schema_validation.py   loads protocol/schemas/*.json from disk, validates against them
hub/auth.py        argon2 API-key hashing/verification/rotation/expiry + request-scoped org_id
hub/abuse.py       size limits, per-org rate limiting, a spam heuristic -> quarantine
hub/audit.py       append-only audit-log writes (who did what, no secrets, no content)
hub/observability.py  JSON logging, request-id correlation, /healthz + /readyz
hub/crud.py        the six tools' actual query logic -- ALWAYS org_id-scoped in SQL
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
rows belonging to `org_b` may ever appear in any of the six tools' responses,
and `get_trace` on a known `org_b` id must 404, never 403 (never confirm the
id exists). `hub/tests/test_tenant_isolation.py` enforces exactly that.

That is in real tension with the product pitch elsewhere in this repo
("CommonTrace ... turning AI agent experience into validated, reusable
lessons ... across organizations"): a Hub where every org only ever sees its
own traces isn't a commons at all.

This implementation resolves that tension conservatively, not by picking a
side silently: **every read path is unconditionally scoped to the caller's
own `org_id` today.** `Trace.shared_with_commons` exists as a column in
`hub/models.py` and is written (always `False` for now) but is never read by
any query in `hub/crud.py`. It's there so a future "opt-in commons" milestone
— an org explicitly marking a trace shareable, and `search_traces` gaining
an opt-in `include_commons` mode once moderation/trust for cross-org exposure
is designed — doesn't require a schema migration, but no cross-org sharing
happens until that's built and reviewed on its own merits. Get the walls
right first; open doors deliberately.

One consequence worth knowing: because `vote_trace`/`amend_trace`/
`get_trace` are all scoped to the caller's own org, an org can currently only
vote on or amend its own traces. That's a correct, if narrow, MVP — not a
bug — under the isolation-first resolution above.

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

### Container image: present, but not yet built by its authors

`Dockerfile` and `docker-compose.yml` now exist at the repo root (an earlier
revision of this file said they deliberately did not — that is no longer
true). One caveat worth stating plainly: **they have never been built or
run.** The environment they were written in had no Docker daemon, so they
are reviewed-but-unverified. CI has a `docker-build` job that builds the
image and asserts the container serves `/healthz`; until you've seen that
pass, treat the image as unproven and prefer the from-a-checkout path above.

Everything else in this README describes behavior covered by `hub/tests/`
against a real Postgres.

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
