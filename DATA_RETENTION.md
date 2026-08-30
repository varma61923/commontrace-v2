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
| `organizations` | Org id + display name. Plus, while a self-service whole-account deletion is pending: a hashed confirmation token and its request/expiry timestamps (never the raw token -- see §3). |
| `api_keys` | Argon2 hash of each org's API key (never the raw key), a non-secret lookup prefix, issuance/revocation/last-used timestamps. |
| `traces` | The `Trace` object (title, context_text, solution_text, tags, agent_type, extensions, outcome, ...) plus `org_id`, `quarantined`/`quarantine_reason` (abuse-control state), `trust`/`retrievals`/`depth` (Hub-computed). For Knowledge Base entries only: `commons_votes`/`commons_review_after` (standing inputs) and, once an operator withdraws one, `commons_retracted_at`/`commons_retraction_reason` — see §5 on why a retracted entry is retained rather than deleted. |
| `votes` | Up/down votes + optional feedback, per (trace, org). |
| `trace_relations` | AMENDS/SUPERSEDED_BY edges created by `amend_trace`. |
| `kb_submissions` | An org's proposed Knowledge Base entries (title, context_text, solution_text, tags, agent_type, rationale) plus `org_id`, `status` (pending/approved/rejected), reviewer identity and timestamp, and -- once decided -- `resulting_trace_id`/`credit_awarded`. Never read by `commons_overlap`/`commons_search`; see `hub/models.py:KnowledgeBaseSubmission`. |

Every read path is scoped to the calling org's own `org_id` at the query
layer (`hub/crud.py`); see `hub/README.md`'s "Tenant isolation vs. the
CommonTrace Knowledge Base" for the one deliberate exception (an
optional, operator-curated corpus, never another customer's own data, and
never a customer's own submission either unless an operator republishes it).

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
  is a business decision this document does not make.

## 3. How an org requests deletion

- **Local tier:** delete the relevant files under `memory/` (or the whole
  repo). There is no CommonTrace-side record to also purge, since none is
  kept outside those files.
- **Hub tier (`hub/`):** revoking an org's access is implemented
  (`python -m hub.manage revoke-key <key_id>` — see `hub/README.md`).
  Permanently deleting data is implemented at two trust levels:

  1. **Self-service, an org's own API key** (`commontrace account
     delete-trace <id>` / `delete_trace` MCP tool): deletes one trace and
     its entire amendment chain, immediately. **Whole-account deletion**
     (`commontrace account request-deletion` then `confirm-deletion
     <token>` / `request_account_deletion` + `confirm_account_deletion`
     MCP tools) is two calls, not one: `request_account_deletion` deletes
     nothing and only returns a one-time confirmation token plus a
     mandatory minimum wait (`crud.DELETION_GRACE_SECONDS`, 5 minutes)
     before `confirm_account_deletion` will accept it — specifically so a
     single compromised API key cannot wipe an org's entire history with
     no window for anyone to notice. `cancel_account_deletion` stands a
     pending request down, no token required. See
     `hub/crud.py:request_org_deletion` and `hub/README.md`'s
     "Self-service deletion" section.
  2. **Operator-CLI, database-access trust level**
     (`python -m hub.manage purge-trace <trace_id>` /
     `purge-org <org_id>`): the same operations, for when an org has lost
     its own keys or an operator needs to act without one. `purge-org`
     cascades to that org's `api_keys`/`traces`/`votes`/`kb_submissions`
     via FK `ondelete=CASCADE`; both this and self-service deletion clean
     up any `trace_relations` row that would otherwise dangle, and both
     walk and delete a trace's entire amendment chain (every trace it
     supersedes and every trace that supersedes it) rather than just the
     one id given — `amend_trace` creates a new row that carries most of
     the original's content forward, so scoping a purge to a single link
     in that chain would leave the same content sitting in its neighbors.

  Both trust levels are irreversible, with no soft-delete and no undo.

## 4. Does deleting an org's trace ever have to reach into another org's data?

**No, and that is by design rather than a gap left open.** An earlier
design considered letting one org opt a trace into a shared corpus other
orgs' queries could match against — which would have raised exactly the
hard question this section used to pose (org A deletes a trace; org B
already downloaded a lesson derived from it; now what?). That design is
retired before ever shipping to a real deployment. See
`hub/commons.py`'s module docstring and `hub/plans.py` "why there is no
org-to-org sharing here" for the full reasoning: it does not make sense
for orgs to share their IP and data with each other, and doing so has an
adverse-selection problem with no fix.

What exists instead is the **CommonTrace Knowledge Base**: a single corpus
the *operator* authors and curates, either directly (`hub/manage.py
commons-seed`) or by accepting a community proposal
(`approve-submission`). No customer-facing tool can write a
`commons_source == "seed"` row, and no customer's own trace is ever
directly in it — `commons_overlap`/`commons_search` filter explicitly on
that column, which only those two operator-run paths ever set.
Consequently:

- Deleting an org's own trace (`purge-trace`/`purge-org`) is a clean, local
  operation exactly as described in §1–3. There is no other org's
  `memory/lessons/` file that could have derived anything from it, because
  no other org's tooling — and no Knowledge Base query — ever saw it.
- A `submit_kb_entry` proposal is a separate row (`kb_submissions`, §1)
  from the moment it is created, not a promoted `Trace` — deleting an
  org's traces never touches its submissions, and vice versa.
- The only content any org's query can ever draw on beyond its own data is
  what the operator wrote or approved into the Knowledge Base. Withdrawing
  a Knowledge Base entry is an operator decision, not a customer deletion
  request, and `python -m hub.manage kb-retract` is how it is made
  (`kb-restore` reverses it, `kb-review` lists what is worth looking at).
  Because the content is the operator's own, withdrawing an entry raises
  none of the questions §3 answers about customer data — nothing cascades
  and no other org's store is affected. Correcting an entry *in place*
  still has no command; see §5.
- `vote_trace` lets any org vote on a Knowledge Base entry (feedback on
  the operator's content), and that vote is retained the same way any
  other row is (§2). It is never a customer's own trace data crossing an
  org boundary.

## 5. Related open questions for whoever operates a `hub/` deployment

- ~~`hub/` still has no API/self-service data-deletion path~~ **Resolved:**
  `delete_trace` (immediate, self-service) and `request_account_deletion` /
  `confirm_account_deletion` (two-call, mandatory delay, self-service) now
  exist alongside the operator-CLI `purge-trace`/`purge-org` path (§3).
  What remains open: this document does not commit to a deletion-request
  **SLA** (how quickly must a request be honored, in what jurisdiction) --
  the mechanism exists and is instant once called, but "instant when
  called" is an engineering fact, not a contractual one, and only whoever
  operates a real deployment can make that commitment.
- Where would a real deployment's Postgres actually be hosted, under what
  jurisdiction, and with what backup/retention configuration? Nothing in
  `hub/` prescribes this — it is deploy-target-specific and unset in
  `hub/.env.example`.
- An org has no way to withdraw a `submit_kb_entry` proposal once sent --
  only an operator's `approve-submission`/`reject-submission` decides it.
  A pending submission is never visible to anyone but the submitting org
  and the operator either way (§1), so the exposure this would address is
  narrow, but a "you can always take back what you have not published
  yet" self-service path is a reasonable expectation nothing here
  currently meets.
- ~~There is no CLI command to correct or remove a single Knowledge Base
  entry after `commons-seed` has loaded it~~ **Resolved:**
  `python -m hub.manage kb-retract <trace_id> [reason]` withdraws one
  entry (and `kb-restore` reverses it), with `kb-review` listing which
  entries an operator should be looking at. Note what retraction is and
  is not, since this document is about retention: it sets
  `Trace.commons_retracted_at`, which removes the entry from every
  Knowledge Base read path, and **retains** the row, its votes, and its
  hit history — deliberately, because "how many fleets did we serve this
  to before we pulled it" is answerable only from that data. It is
  un-publishing, not deletion. `purge-trace` remains the deletion path
  (§3), and nothing about retraction changes §4's answer: a Knowledge
  Base entry is the operator's own content, so withdrawing one never
  reaches into any customer's data.
  What remains open: there is still no command to *correct* an entry in
  place. The workflow is retract-and-reseed, which changes the trace id
  and resets `commons_hits`, so an entry's delivered-value history does
  not survive an edit.
