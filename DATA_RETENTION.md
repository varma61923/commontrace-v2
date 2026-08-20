# Data Retention

This document describes what CommonTrace stores, where, and for how long,
and what happens when an organization stops using it. It covers what exists
today; where the honest answer is "not built yet" or "not this repo's call
to make," that is stated explicitly rather than guessed at.

## 1. What is stored, and where

Per `protocol/PROTOCOL.md` §5, there are two conformance tiers:

### Local tier (implemented, this repo)

Flat Markdown files with YAML frontmatter, written to `memory/` inside
whatever directory the user ran `commontrace init` in:

| Path | Contents |
|---|---|
| `memory/traces/*.md` | Captured task episodes: what happened, context, outcome. May include the raw text the user/agent supplied via `commontrace capture` (task descriptions, error text, etc.). |
| `memory/lessons/*.md` | Distilled, reusable rules derived from traces. |
| `memory/episodes/*.md` | Code-review profile's run log (Agent A/B/Omega/Lambda pipeline transcript metadata). |
| `memory/attention/index.npz` | Sentence-embedding vectors of lesson text, for semantic retrieval. Derived data, regenerable from `memory/lessons/`. |
| `memory/benchmark_reports/`, `memory/alpha_telemetry.jsonl` | Benchmark run history and retrieval telemetry, when those features are used. |

This data lives entirely on the machine/repo where the user runs the CLI.
CommonTrace itself makes no network call to store it anywhere — it is a
local file format, subject to whatever the user's own repo hosting,
backup, and access-control policies already are. There is nothing for
*this project* to retain or delete on the user's behalf at the local tier;
deleting the files (or the repo) is deletion.

### Hub tier (implemented in `hub/`; not currently deployed anywhere)

A real Hub server now exists in this repository (`hub/`, see
`hub/README.md`), exposing the six MCP tools `protocol/PROTOCOL.md` §5
describes (`search_traces`, `contribute_trace`, `get_trace`, `vote_trace`,
`amend_trace`, `list_tags`) over Postgres, with an `org_id` on every trace
row. What follows describes what that codebase actually does — it is not a
claim that any particular deployment of it exists or holds real customer
data today. If/when an operator deploys `hub/`, this section is the accurate
description of that deployment's storage and isolation behavior; if a
*different* Hub implementation is deployed instead, this section does not
apply to it and needs to be re-verified against that system.

| Table (`hub/models.py`) | Contents |
|---|---|
| `organizations` | Org id + display name. |
| `api_keys` | Argon2 hash of each org's API key (never the raw key), a non-secret lookup prefix, issuance/revocation/last-used timestamps. |
| `traces` | The `Trace` object (title, context_text, solution_text, tags, agent_type, extensions, outcome, ...) plus `org_id`, `quarantined`/`quarantine_reason` (abuse-control state), `trust`/`retrievals`/`depth` (Hub-computed). |
| `votes` | Up/down votes + optional feedback, per (trace, org). |
| `trace_relations` | AMENDS/SUPERSEDED_BY edges created by `amend_trace`. |

Every read path is scoped to the calling org's own `org_id` at the query
layer (`hub/crud.py`); see `hub/README.md`'s "Tenant isolation vs. the
cross-org commons pitch" for why cross-org visibility is not yet automatic
even though the product's positioning describes cross-org learning.

## 2. How long data is kept

- **Local tier:** indefinitely, until the user deletes the files. CommonTrace
  has no automatic expiry, archival, or purge job for anything under
  `memory/`. `status: archived` on a lesson (see `protocol/PROTOCOL.md` §4)
  is a soft, in-band marker the tooling can filter on — it does not delete
  or move the underlying file.
- **Hub tier (`hub/`):** indefinitely as well — the same "no automatic
  expiry" is true here. Nothing in `hub/` runs a retention/purge job; a row
  in `traces`/`votes`/`api_keys` persists until explicitly deleted. What
  "indefinitely" *should* mean for a real deployment holding paying
  customers' data (30 days after contract end? 1 year? never, until asked?)
  is a business decision — see §4.

## 3. How an org requests deletion

- **Local tier:** delete the relevant files under `memory/` (or the whole
  repo). There is no CommonTrace-side record to also purge, since none is
  kept outside those files.
- **Hub tier (`hub/`):** revoking an org's access is implemented
  (`python -m hub.manage revoke-key <key_id>` — see `hub/README.md`).
  Permanently deleting data is also implemented, at the operator-CLI level:
  `python -m hub.manage purge-trace <trace_id>` and `purge-org <org_id>`
  (the latter cascades to that org's `api_keys`/`traces`/`votes` via FK
  `ondelete=CASCADE`; both clean up any `trace_relations` row that would
  otherwise dangle). `purge-trace` also walks and deletes the trace's
  entire amendment chain (every trace it supersedes and every trace that
  supersedes it) rather than just the one id given: `amend_trace` creates
  a new row that carries most of the original's content forward, so a
  purge scoped to a single link in that chain would leave the same
  content sitting in its neighbors. Both are irreversible and require the same
  database-access trust level as every other `hub/manage.py` command —
  there is still **no self-service or API-level deletion path**: none of
  the six Hub MCP tools (`search_traces`, `contribute_trace`, `get_trace`,
  `vote_trace`, `amend_trace`, `list_tags`) includes a `delete_trace` or
  `forget_org` operation, and an org's own API key cannot delete anything.
  That's a deliberate scope boundary, not an oversight: letting a single
  API key wipe an org's entire history with no confirmation step is a real
  feature with its own authorization design questions this pass didn't
  make (see `hub/README.md`'s Operator CLI section).

## 4. What happens to lessons already derived from an org's contributed traces

This is the hardest question and is explicitly **not** answered here, because
it is a business/legal decision, not an engineering one:

> If organization A contributes a trace to the Hub, and organization B's
> agent later reads a lesson that was distilled (possibly by an automated
> Curator, possibly by a human) from that trace, and organization A then
> requests deletion — what happens to:
> 1. The original trace (straightforward: delete it).
> 2. The lesson text derived from it, now potentially embedded in B's
>    (and every other org's) local `memory/lessons/` files, already
>    downloaded and possibly acted on.
> 3. Any lesson that merged information from multiple orgs' traces, where
>    "delete this org's contribution" isn't a clean subtraction.

**This needs a decision from whoever owns commercial/legal terms with
Hub-contributing organizations before a customer's data is allowed to flow
into a cross-org "commons."** Candidate positions (not a recommendation,
just the shape of the choice) range from "traces are deletable, lessons
already derived and distributed are not retroactively recalled" (like an
open-source contribution model) to "lessons must be re-derivable/
re-validatable without deleted source traces, with a grace/quarantine
period." Flagging this, not deciding it, is the point of this section.

Note on current scope: `hub/`'s tenant isolation is deliberately strict
today (see §1 and `hub/README.md`) — every read is scoped to the caller's
own `org_id`, so the cross-org scenario above (org B ever seeing a lesson
derived from org A's trace) cannot happen yet in this codebase as shipped.
It becomes live the moment a future "opt-in commons" milestone is built on
top of the `shared_with_commons` column that already exists in
`hub/models.py` but is not yet acted on by any query — this decision should
land *before* that milestone ships, not after.

## 5. Related open questions for whoever operates a `hub/` deployment

- `hub/` still has no API/self-service data-deletion path (§3) — operator-CLI
  purge is implemented (`purge-trace`/`purge-org`), but an org cannot delete
  its own data via its own API key. Deciding whether/how to expose that
  (a `delete_trace`/`forget_org` MCP tool, with what confirmation/
  authorization step) needs to happen before this document can state a real
  self-service deletion SLA.
- Where would a real deployment's Postgres actually be hosted, under what
  jurisdiction, and with what backup/retention configuration? Nothing in
  `hub/` prescribes this — it is deploy-target-specific and unset in
  `hub/.env.example`.
- §4's cross-org deletion question needs an answer before (not after) the
  "opt-in commons" milestone in `hub/README.md` ships.
