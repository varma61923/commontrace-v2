# The CommonTrace Protocol

**Version:** 2.0.0 | **Status:** stable | **Scope:** agent-agnostic

> This document is the canonical, implementation-independent specification of
> CommonTrace. `SKILL.md` in the repo root is *one* conformant implementation
> of this protocol — a reference profile for coding agents (the "code-review
> profile"). Support, sales, HR, marketing, or any other agent fleet can
> implement this protocol without adopting that profile's double-review
> pipeline at all.

## 1. Why this document exists

The repo previously had one hard-coded shape: a coding agent's double-review
run (Alpha → A → B → Omega → Lambda), with a closed 7-value `domain` enum and
episode fields like `commit_sha` and `verdict` baked into the core schema.
That shape doesn't fit a Support agent closing a ticket, a Sales agent
handling an objection, or an HR agent screening a resume — but the underlying
idea (capture what happened, distill a validated rule, reinject it before the
next decision) transfers completely. This document splits the two apart:
a small, universal **protocol core** (this file + the two schemas next to
it), and any number of **profiles** that add domain-specific fields on top
without touching the core.

## 2. The pipeline (seven stages, one loop)

The table below is normative. Any restatement of the pipeline elsewhere in
this repository (AGENTS.md, SKILL.md, `commontrace install`'s generated
files) reproduces these seven stage names verbatim and in this order.

```
Capture → Structure → Extract → Validate → Store → Inject → Measure
                         ↑                                     |
                         └─────────────────────────────────────┘
```

`Validate` is a stage, not a formality: it is the human gate where a
candidate lesson becomes one that can affect a live decision. Omitting it
from a restatement of the pipeline misrepresents the protocol's central
safety property.

| Stage | What happens | Who does it (generic role) | Reference impl |
|---|---|---|---|
| **Capture** | An agent finishes a task; the raw experience (what it tried, what happened) is recorded. | Producer | Any agent, any platform |
| **Structure** | Raw experience is normalized into a `Trace` (§3). | Producer / Curator | `commontrace capture` |
| **Extract** | Patterns across traces are distilled into candidate `Lesson`s (§4) — generalized, reusable rules. | Curator | Omega (code-review profile) |
| **Validate** | Candidate lessons are audited before they can affect a live decision (formal quality, non-duplicate, generalization, importance). | Validator | Lambda (code-review profile) |
| **Store** | Validated lessons persist in a queryable store — local files, or the Hub. | Store (§5) | `memory/` or CommonTrace Hub |
| **Inject** | Before an agent acts, relevant lessons are retrieved and placed in its context. | Retriever | Alpha (code-review profile) |
| **Measure** | Compare behavior with vs. without injected lessons (repeated-error rate, resolution rate, escalation rate, cost). | — | `commontrace bench` |

Everything left of "Store" is about **generation quality**; everything right
of it is about **retrieval quality**. Both are independently measurable —
that split is what `commontrace bench`'s three axes
(`lesson_quality`, `implicit_retrieval`, `transfer_gap`) actually measure,
and it holds regardless of agent type.

## 3. Core object: `Trace`

The atomic unit of captured experience. Schema: [`schemas/trace.schema.json`](schemas/trace.schema.json).

A `Trace` is deliberately small and generic: `title`, `context_text`,
`solution_text`, `tags`, `agent_type`. This is not an invented shape — it is
the **exact object already served by the production CommonTrace Hub** (the
`search_traces` / `get_trace` / `contribute_trace` / `vote_trace` /
`amend_trace` MCP tools). Local traces and Hub traces are the same object at
different sync states; there is no separate "local trace format" to
translate. Anything a profile needs that doesn't generalize (a git commit
SHA, a support ticket ID, a CRM deal stage) goes in `extensions`, namespaced
under `profile`.

Traces are content-addressable knowledge, not a session log: a good trace
should read as a self-contained answer to "what would I need to know to not
repeat this," independent of who hit the situation or when.

## 4. Core object: `Lesson`

The governance wrapper that sits between raw `Trace`s and injection into a
live decision. Schema: [`schemas/lesson.schema.json`](schemas/lesson.schema.json).

A `Lesson` adds exactly what a `Trace` doesn't have: `domain` (open
vocabulary, see §7), `importance` (1-5, see §6), `applies_when` /
`do_not_apply_when` (the activation condition a Retriever checks before
injecting it), `status` (`active` / `review` / `archived`), and
`source_traces` (provenance). Not every deployment needs the Lesson layer —
a fleet can contribute `Trace`s straight to the Hub and rely on
`search_traces` for retrieval. Lessons exist for fleets that want an
explicit approval gate before anything reaches a live decision, matching the
"approve lessons before deployment" requirement of a production pilot.

## 5. Store: two conformance tiers

| Tier | Transport | Scope | Reference impl |
|---|---|---|---|
| **Local** | Flat files (Markdown + YAML frontmatter) | One fleet, one repo/org | `memory/lessons/` (any `agent_type`), `memory/traces/` (generic `Trace` capture, any `agent_type`), `memory/episodes/` (code-review profile's run log) |
| **Hub** | MCP (`mcp__commontrace__*` tools: `search_traces`, `contribute_trace`, `get_trace`, `vote_trace`, `amend_trace`, `list_tags`) | Cross-org, cross-fleet, cross-agent-vendor | CommonTrace Hub (already in production) |

A client only has to implement **Local** to be protocol-conformant. Hub
conformance is additive: `commontrace sync` (see the CLI, §8) promotes local
`active` lessons to Hub traces via `contribute_trace`, and pulls Hub
search results back into the local store as candidate lessons awaiting
validation. Nothing about the local schema had to change to make this work —
that convergence is the point of aligning `Trace` with the live Hub shape in
§3 instead of inventing a parallel one.

## 6. Roles (generalized)

The code-review profile's five sub-agents are one instantiation of five
generic roles. Any agent-agnostic implementation needs at least Producer;
the rest are optional depth you add as a fleet matures.

| Generic role | Code-review profile name | Minimum requirement | Generic CLI reference (any `agent_type`) |
|---|---|---|---|
| **Producer** | Agent A (implementer) | Does the actual task. Always present — this is just "the agent." | — (the agent itself) |
| **Reviewer** *(optional)* | Agent B | Independent audit of the Producer's output before it ships. Coding-specific value; a Support/Sales agent may skip this. | — |
| **Curator** | Omega | Turns traces into candidate lessons. Can be automated (an LLM pass) or manual. | `commontrace distill` — heuristic (word-overlap clustering, no LLM call) pattern-finder; writes candidates at `status: review`, never `active`. |
| **Validator** | Lambda | Approves/rejects candidate lessons before they go live. Can be automated, or a human approval step (the pilot's "approve lessons before deployment"). | `commontrace lesson approve\|reject` — the only way a `review` lesson becomes `active` or `archived`; refuses to act on a lesson not already in `review`. |
| **Retriever** | Alpha | Surfaces relevant lessons before the Producer acts. | `commontrace query` — semantic (optional `[attention]` extra) with an automatic pure-Python lexical fallback, so retrieval works with only the core install. |

## 7. Taxonomy (open, not closed)

`domain` and `tags` are open strings at the protocol level — no fixed enum
is validated. The code-review profile's historical 7-value domain set
(`git-safety`, `cuda-gpu`, `refactor`, `testing`, `subagents`, `performance`,
`other`) remains valid; it's a subset, not the whole space. Recommended
starter domains per `agent_type`, so a multi-agent-type fleet's `INDEX.md`
stays legible:

| `agent_type` | Starter domains |
|---|---|
| `code` | `git-safety`, `refactor`, `testing`, `subagents`, `performance`, `cuda-gpu`, `other` |
| `support` | `escalation`, `refunds`, `troubleshooting`, `tone`, `known-issues` |
| `sales` | `objection-handling`, `pricing`, `qualification`, `competitor`, `follow-up` |
| `hr` | `screening`, `compliance`, `onboarding`, `policy` |
| `marketing` | `messaging`, `compliance`, `channel`, `brand-voice` |
| `custom` | fleet-defined |

## 8. Client & installation surface

"Client-installable" means two independent things, both covered:

1. **Installing CommonTrace itself** into a project/fleet — currently
   `pip install -e .` from a repo checkout (this package is not yet published
   to PyPI; `pip install commontrace` is the intended path once it is — see
   `README.md`), then `commontrace init --agent-type <type>` scaffolds a
   local store (§5, Local tier). See `commontrace/` (the CLI package) and
   `pyproject.toml`.
2. **Wiring a specific agent platform** to read/write that store —
   `commontrace install --target claude-code|cursor|devin|windsurf|generic-mcp`
   writes the platform-native integration file into the *destination
   project* (e.g. `.claude/skills/commontrace/SKILL.md`, `.cursor/rules/`)
   — see `commontrace/commands/install_cmd.py` for the full target list and
   what each one writes.

Because the Hub tier speaks MCP — already supported natively by Claude Code,
Cursor, Devin, Windsurf, and any OpenAI-Agents/generic MCP client — "agent
agnostic" is not aspirational copy. Any MCP-capable agent can attach to the
Hub today with a config block, no CommonTrace-specific SDK required. Local
tier remains file-based for agents that can only read/write files.

## 9. Versioning & extension mechanism

- This protocol is versioned independently of any single profile
  (`SKILL.md`'s code-review profile is versioned separately, currently v2.3).
  Breaking changes to `trace.schema.json` / `lesson.schema.json` bump the
  major version; additive, optional fields (e.g. `Trace.outcome`, §11) bump
  the minor version. The protocol is at **2.0.0** — the v2 product baseline
  (matching the `commontrace-v2` repo), unifying what had drifted into two
  numbers (package `1.0.0` vs. protocol `1.1.0`) into one version that the
  package, the protocol, and the CLI (`commontrace --version`) all report
  identically. It is not a breaking schema change: a `Trace`/`Lesson` written
  under v1.x remains valid under 2.0.0.
- A profile MUST declare itself via the `profile` field on `Trace` and put
  everything that doesn't generalize under `extensions`. A consumer that
  doesn't recognize a `profile` value MUST still be able to read `title`,
  `context_text`, `solution_text`, `tags`, `agent_type` and safely ignore
  `extensions`.
- New profiles (a "support-escalation" profile, a "sales-call" profile, etc.)
  are added by writing a new `SKILL.md`-equivalent spec and, if useful, a
  JSON Schema fragment for their `extensions` shape — never by editing
  `trace.schema.json` or `lesson.schema.json`.

## 10. Importance rubric

Carried over unchanged from the code-review profile because it is already
agent-agnostic (it's about the shape of the consequence, not the domain):

```
5 (showstopper) — without this lesson, the whole task class fails or causes
                   data loss / security breach / (in a business-agent
                   context) a compliance violation or contract-losing error.
4 (critical)     — ignoring it → high probability of major rework, a
                    subtle hard-to-detect bug, or a customer-visible mistake.
3 (useful)       — avoids a common anti-pattern or methodological trap.
2 (minor)        — valid but limited to a specific sub-case.
1 (anecdotal)    — interesting, not very actionable on its own.
```

## 11. Pilot outcome metrics

CommonTrace's pitch to a prospective fleet is "measured impact," not just
retrieval — a pilot needs to show, in the fleet's own business terms, that
injecting lessons changed outcomes. These are five specific, computable
metrics, deliberately distinct from the internal §2 generation/retrieval
axes (`lesson_quality`, `implicit_retrieval`, `transfer_gap`, computed by
`commontrace bench`), which measure whether the *protocol
machinery itself* is healthy (is Omega proposing good lessons, is Alpha
retrieving the right ones). The pilot metrics below measure whether the
*fleet's behavior* actually changed — the thing a customer is paying for.

| Metric | Definition | Backing field(s) on `Trace.outcome` |
|---|---|---|
| **Repeated-error rate** | % of traces marking a recurrence of a previously captured failure. | `repeated_error` |
| **Resolution rate** | % of traces whose underlying task reached a successful conclusion (closed ticket, won deal, passed suite, approved asset — meaning is `agent_type`-specific). | `resolved` |
| **Escalation rate** | % of traces that required human escalation. | `escalated` |
| **Frustration rate** | % of traces with an explicit negative signal (complaint, downvote, low CSAT). | `frustration_signal` |
| **Token / inference cost** | Mean `tokens_used` and `llm_calls` per trace. | `tokens_used`, `llm_calls` |

All five are ratios or means over traces where the backing field is
non-null; a trace that never recorded an `outcome` simply doesn't
contribute — it is not counted as a negative result. `outcome.baseline`
splits traces into a pre-CommonTrace baseline window and a post-injection
window, so a deployment can report a before/after change for each metric:
compute each metric once per `baseline` value and diff. This is a standing
production measurement, not a one-off evaluation — the same split answers
"did adopting this help?" and, months later, "is it still helping?" `commontrace capture` accepts `--resolved` /
`--escalated` / `--repeated-error` / `--frustration` (each with a `--not-*`
counterpart, e.g. `--not-escalated`, so a definite "no" can be recorded, not
just "yes" or "unknown" — a field's rate denominator only counts traces
where it was actually set one way or the other), plus `--tokens-used`,
`--llm-calls`, and `--baseline` to populate this at capture time;
`commontrace bench --pilot`
computes and reports the five metrics, baseline-vs-current, from whatever
`memory/traces/` currently holds. Populating `outcome` is optional and
additive — a fleet that only wants the generation/retrieval axes from §2
can ignore it entirely.

## 12. What this document does not standardize (yet)

- The Curator/Validator prompts themselves (how an LLM turns a trace into a
  candidate lesson) — that's profile-specific by design.
- Hub authentication/multi-tenancy details — out of scope for a protocol
  spec; the Hub already exists and is reachable over MCP.
- Non-file local storage backends (e.g. SQLite) — nothing in this spec
  precludes one; `commontrace/paths.py` + `commontrace/trace_io.py` /
  `commontrace/frontmatter.py` are the current file-backed read/write seam a
  second backend would need to sit behind (no such backend exists yet —
  there is no `commontrace/store.py` in the codebase today).
