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

### Hub tier (status unresolved — see §4)

`protocol/PROTOCOL.md` §5 describes a Hub reached over MCP
(`search_traces`, `contribute_trace`, `get_trace`, `vote_trace`,
`amend_trace`, `list_tags`) and lists it as "already in production." This
repository's own client code disagrees: `commontrace/commands/sync_cmd.py`
makes no network call and only prints instructions for how an MCP-capable
agent could bridge to a Hub manually. No Hub server implementation, and no
schema field for organization/tenant identity (`org_id` or similar), exists
anywhere in `protocol/schemas/` or `commontrace/` today.

**This document cannot state a real retention period, deletion SLA, or
storage location for the Hub tier, because there is no verified live system
to describe.** Whoever owns the actual Hub deployment (if `PROTOCOL.md`'s
"already in production" claim is accurate) needs to either supply this
document with real answers, or `PROTOCOL.md` needs to be corrected to stop
describing it as production infrastructure. See `DATA_RETENTION.md` §4 for
the specific decision this blocks.

## 2. How long data is kept

- **Local tier:** indefinitely, until the user deletes the files. CommonTrace
  has no automatic expiry, archival, or purge job for anything under
  `memory/`. `status: archived` on a lesson (see `protocol/PROTOCOL.md` §4)
  is a soft, in-band marker the tooling can filter on — it does not delete
  or move the underlying file.
- **Hub tier:** unknown — see §1.

## 3. How an org requests deletion

- **Local tier:** delete the relevant files under `memory/` (or the whole
  repo). There is no CommonTrace-side record to also purge, since none is
  kept outside those files.
- **Hub tier:** no deletion mechanism exists in this codebase — the six Hub
  MCP tools listed in `protocol/PROTOCOL.md` §5 do not include a
  `delete_trace` or `forget_org` operation. If a real Hub exists elsewhere,
  it needs its own documented deletion path; this repo cannot speak for it.

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
Hub-contributing organizations before a Hub is built or a customer's data
touches one.** Candidate positions (not a recommendation, just the shape of
the choice) range from "traces are deletable, lessons already derived and
distributed are not retroactively recalled" (like an open-source contribution
model) to "lessons must be re-derivable/re-validatable without deleted
source traces, with a grace/quarantine period." Flagging this, not deciding
it, is the point of this section.

## 5. Related open questions for whoever resolves §1/§4

- Does a real CommonTrace Hub already exist in production, contradicting
  this repo's own `sync_cmd.py`? (`protocol/PROTOCOL.md` §5 says yes; this
  repo's code says no network client exists to reach one.)
- If yes: where is its data hosted, under what jurisdiction, and what
  retention/deletion SLA does it already operate under today?
- If no: retention/deletion policy should be designed alongside the Hub's
  schema (which will need an `org_id` concept that doesn't exist yet) rather
  than retrofitted after data is already flowing.
