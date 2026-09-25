# commontrace

> **Version:** 2.0.0 | **Code-review reference profile:** v2.3 (versioned independently, see [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md#9-versioning--extension-mechanism)) | **Status:** trial-ready | **Python:** 3.10+

CommonTrace is an **agent-agnostic protocol** for turning agent experience into
validated, reusable lessons: Capture → Structure → Extract → Validate → Store →
Inject → Measure. The full spec lives in [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md).

This repo ships two things:

1. **The `commontrace` CLI** (`pip install -e .`) — client-installable, works with
   any agent fleet (code, support, sales, HR, marketing, ...), and can wire a local
   store into Claude Code, Cursor, Devin, Windsurf, or any generic MCP client. It
   also bridges to the **CommonTrace Hub** (a self-hostable, multi-tenant trace
   store reachable over MCP — `search_traces`, `contribute_trace`, `get_trace`,
   `vote_trace`, `amend_trace`, `list_tags`, `fleet_outcomes` to ask whether
   your fleet's numbers have actually improved, `holdout_assign`/
   `record_occasion_outcome` to prove it causally, plus an optional Knowledge
   Base — `commons_overlap`, `commons_search` to consult it, `submit_kb_entry`/
   `list_my_kb_submissions` to propose an entry for operator review; server
   implementation and setup in [`hub/`](hub/README.md), not a hosted service
   run by this project). Every org's own traces stay private to that org; no
   org's data is ever exposed to another org.
2. **A reference implementation for coding agents** (`SKILL.md`) — the
   double-review pipeline (**Implementer A** + independent **Reviewer B**, iterating
   until the task passes) that this whole protocol was distilled from. One profile
   among possibly many; support/sales/HR/marketing fleets don't need it to use
   CommonTrace.

Works with **any agent platform** — Devin, Claude Code, Cursor, Windsurf, OpenHands,
or custom orchestrators — because the local store is plain files and the Hub speaks
MCP, which all of the above already support natively.

---

## Table of Contents

1. [Quick Start — CLI (any agent type)](#quick-start--cli-any-agent-type)
2. [Quick Start — Agents with no terminal (MCP)](#quick-start--agents-with-no-terminal-mcp)
3. [Quick Start — Code Agent reference profile](#quick-start--code-agent-reference-profile)
4. [How the Reference Pipeline Works](#how-it-works)
5. [Architecture](#architecture)
6. [Configuration](#configuration)
7. [Memory System](#memory-system)
8. [Benchmark](#benchmark)
9. [Outcome Metrics](#outcome-metrics)
10. [The CommonTrace Knowledge Base](#the-commontrace-knowledge-base)
11. [Deploying to Production](#deploying-to-production)
12. [File Layout](#file-layout)
13. [Requirements](#requirements)

---

## Quick Start — CLI (any agent type)

### 1 — Install

```bash
pip install -e .                    # from a repo checkout — installs the `commontrace` command
# core install has one dependency (PyYAML); the semantic attention layer is optional:
pip install -e ".[attention]"
```

### 2 — Bootstrap a store for your fleet

```bash
commontrace init --agent-type support        # any field: code, sales, hr, marketing,
                                             # ops, robotics, legal, ... (open taxonomy)
commontrace doctor                            # sanity-check the environment
```

**Optional: start from your existing traces.** No infrastructure
replacement — bulk-import a JSONL or CSV export from wherever your fleet's
history already lives (a ticketing export, an observability dump, whatever
you can get out as flat rows) instead of starting from zero:

```bash
commontrace import export.jsonl --agent-type support \
  --title-field subject --context-field description --solution-field resolution
commontrace import export.csv --agent-type support --dry-run   # preview first
```

**Coming from LangSmith, Langfuse, Braintrust or OpenTelemetry?** Pass
`--source` and skip the field mapping entirely — those systems export
*nested* rows (the text lives at `inputs.input`, or inside an OTel attribute
list) that no `--context-field` can reach:

```bash
commontrace import runs.jsonl  --source langsmith  --agent-type support
commontrace import traces.jsonl --source langfuse  --agent-type support
commontrace import spans.jsonl  --source braintrust --agent-type support
commontrace import spans.jsonl  --source otel      --agent-type support
```

Each adapter also picks up the outcome the source system *already knows*:
LangSmith's `error`, Langfuse's `scores`, Braintrust's `expected` vs
`output`, an OTel span's status. Those labelled failures are exactly what
distillation clusters on, so a bulk import arrives with its outcomes intact
rather than as undifferentiated text.

Two things these adapters deliberately do **not** do. They never invent: a
row missing its solution is skipped with a reason, never filled with a
plausible placeholder, because one fabricated field repeated ten thousand
times becomes a corpus this product then measures and bills against. And
they never read as success from silence: an OTel `UNSET` status, a
LangSmith run with no `error` field, a Langfuse trace with no recognised
score — all of these import with *no* outcome rather than as a win, because
scoring an uninstrumented fleet as 100% resolved is the most expensive wrong
answer available here.

They read a **file you already have**. No API key, no hostname, no network
call — which means a security reviewer can diff exactly what crosses the
boundary before it does, and it works in an air-gapped environment.

Field names are configurable (`--title-field`/`--context-field`/
`--solution-field`/`--tags-field`/`--id-field`) for a `generic` flat export,
since a real export's column names are whatever the source system calls
them. Rows missing a required field are skipped and reported, not silently
dropped or a hard failure of the whole batch. `resolved`/`escalated`/`repeated_error`/
`frustration_signal`/`tokens_used`/`llm_calls` columns, if present, populate
`Trace.outcome` (§ [Outcome Metrics](#outcome-metrics)) automatically.

**Already emitting OTel spans? Skip the file.** `pip install
'commontrace[otel]'` and attach `CommonTraceSpanExporter` to a
`TracerProvider` you already have — a completed GenAI span becomes a
trace the moment it exports, through the identical parsing and
schema-validated write path `--source otel` uses above:

```python
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from commontrace.otel_exporter import CommonTraceSpanExporter

provider.add_span_processor(BatchSpanProcessor(
    CommonTraceSpanExporter(agent_type="support")
))
```

This adds no instrumentation to your application — it consumes spans
your own code or an existing vendor SDK already produces. A span with
no GenAI attributes is skipped, never an exception: an exporter that
raised into the application it's attached to would take it down for an
unrelated telemetry side-channel.

**Getting your own corpus back out is the other direction of the same
promise.** `commontrace export` is the counterpart to `commontrace import`
— a portable JSONL dump of this store's lessons and/or traces, for a
backup, an inspection pass in a spreadsheet, a customer's own tooling, or
seeding a second store:

```bash
commontrace export --out backup.jsonl                     # everything
commontrace export --kind lessons --status active --out lessons.jsonl
commontrace export --kind traces > traces.jsonl            # composes with shell redirection
```

Exported trace rows are written in exactly the flat shape `commontrace
import`'s generic mapping already reads (title/context/solution/tags/id,
outcome fields inlined) — `commontrace export --kind traces` on one store
followed by `commontrace import` on another is a real, tested round trip,
with no field-mapping flags needed. Lesson rows carry this store's own
frontmatter/body — there's no bulk lesson importer today (a real one needs
its own slug-collision and approval-status semantics, a separate project),
so a lesson row is documented as this store's interchange format, not
implied to be import-ready.

### 3 — Wire it into your agent platform

```bash
commontrace install --target claude-code      # writes .claude/skills/commontrace/SKILL.md
commontrace install --target cursor           # writes .cursor/rules/ + a Hub MCP config template
commontrace install --target devin            # writes .devin/skills/commontrace/SKILL.md
commontrace install --target windsurf         # writes .windsurf/rules/
commontrace install --target generic-mcp      # Hub MCP config template for any other MCP client
```

**Install-target support matrix** — what each target actually writes, and how far that's
been verified. "Format verified against docs" means the generated file's structure (valid
JSON where JSON is expected, correct frontmatter shape, etc.) was checked against that
platform's publicly documented config convention — not that the file was loaded into the
running product. No target in this table has been live-tested inside the actual platform;
none of that is claimed below.

| Target | File(s) written | Format verified against docs | Live-tested in the platform |
|---|---|---|---|
| `claude-code` | `.claude/skills/<name>/SKILL.md` (copies the repo's `SKILL.md` if found, else a generic pointer skill) — YAML frontmatter (`name`, `description`) + Markdown body | Yes — matches Claude Code's documented Skill file shape | No — not loaded into a running Claude Code session |
| `cursor` | `.cursor/rules/commontrace.mdc` (YAML frontmatter: `description`, `alwaysApply`) + `commontrace.hub.mcp.json.example` (`mcpServers` block) | Yes — `.mdc` frontmatter matches Cursor's documented Project Rules format; the MCP file is valid JSON with the `mcpServers` shape Cursor's `mcp.json` uses | No — not loaded into a running Cursor instance |
| `devin` | `.devin/skills/commontrace/SKILL.md` (same content as `claude-code`) | Partial — file is well-formed Markdown+frontmatter, but Devin's skill-loading convention is less publicly documented than Claude Code's/Cursor's, so this is the least-confirmed target | No — Devin is not available in this environment to confirm it's actually consumed |
| `windsurf` | `.windsurf/rules/commontrace.md` (plain Markdown, no frontmatter) | Partial — matches the commonly documented `.windsurf/rules/*.md` convention, but Windsurf's rules format has changed across releases and isn't independently confirmed here | No — Windsurf is not available in this environment to confirm it's actually consumed |
| `generic-mcp` | `commontrace.hub.mcp.json.example` (`mcpServers` block) | Yes — valid JSON; `mcpServers` is the de facto shape shared by Claude Code/Cursor/Windsurf MCP client configs | No — generic by design, no single product to test against |
| `generic` (fallback) | `COMMONTRACE.md` — plain pointer doc | N/A — no platform-specific format to check against | N/A |

All five real targets (plus the `generic` fallback) were run end-to-end against a scratch
directory as part of this verification pass, including from outside a repo checkout (to
exercise the "no `SKILL.md` on disk" fallback path for `claude-code`/`devin`), and their
output was inspected file-by-file for structural correctness. That inspection caught and
fixed a real bug: `commontrace.hub.mcp.json.example` was previously **not valid JSON** (an
unescaped tool list leaked quotes into a JSON string field) for both `cursor` and
`generic-mcp`.

**Non-Python clients.** Any MCP-capable client already reaches the whole
Hub tool surface without an SDK (that's what `generic-mcp` above
configures). [`sdk/typescript`](sdk/typescript/) is the first dedicated
non-Python client — a thin, typed wrapper (`@commontrace/hub-client`)
over the official MCP TypeScript SDK — for a project that would rather
have typed request/response shapes than hand-write raw tool calls.
Mobile/JVM/.NET clients remain unbuilt.

### 4 — Capture experience and curate lessons

```bash
commontrace capture --title "..." --context "..." --solution "..." --tags a,b --agent-type support
# --agent-id identifies WHICH agent, not what kind -- a fleet of 25 support agents
# shares one --agent-type, so this is what makes the fleet countable (and is what a
# Hub plan's agent limit is metered on). Optional; omitting it is never an error.
commontrace capture --title "..." --context "..." --solution "..." \
  --agent-type support --agent-id support-worker-7
commontrace lesson new --slug lesson_x --description "..." --domain escalation \
  --agent-type support --applies-when "..." --do-not-apply-when "..." --importance 4 \
  --importance-rationale "..."
# `lesson new` scaffolds at status=review, like `distill` does: fill in the
# Rule/Why/How-to-apply sections, then `commontrace lesson approve lesson_x`.
# Only an approved lesson is retrieved, counted as coverage, or pushed to a Hub.
commontrace lesson validate      # checks against protocol/schemas/lesson.schema.json,
                                  # and fails an ACTIVE lesson still full of template text
commontrace trace validate       # checks against protocol/schemas/trace.schema.json
commontrace sync                 # push active lessons + pull search results, if a Hub is configured
commontrace sync --push          # push only
commontrace sync --pull --query "..." --tags a,b   # pull only
commontrace sync --push-traces   # push captured traces + outcome data too (opt-in, off by default)
```

`sync` needs `COMMONTRACE_HUB_URL` + `COMMONTRACE_HUB_API_KEY` (env vars or
`--hub-url`/`--hub-api-key`) pointing at a running Hub — see
[`hub/README.md`](hub/README.md) to run one, and `pip install commontrace[hub-sync]`
for the client dependency. Without those set, `sync` prints setup
instructions instead of failing.

### 5 — Curator/Validator loop (any agent_type, no LLM call required)

The pipeline's "Extract lessons" / "Validate" stages, made concrete for a
fleet that isn't running the code-review profile's Omega/Lambda subagents:

```bash
commontrace distill              # find repeated patterns across memory/traces/,
                                  # write candidate lessons at status=review
commontrace lesson list --status review
commontrace lesson approve lesson_candidate_20260101_1 --rationale "..."
commontrace lesson reject lesson_candidate_20260101_2 --reason "..."
commontrace release cut --reason "Q1 support set"   # a rollback point
```

**A release is what the fleet is running, as one thing.** A lesson has a
slug and its text has a revision; neither answers "what were we running on
Monday", which is what rollback and attribution are actually about.
`release cut` records the active set — content-addressed, append-only,
pinned to each lesson's revision — and `release diff`/`release rollback`
work from there. Cutting from a release the store has moved past is refused
(two curators each approving a lesson would otherwise lose one of the
decisions), and a rollback refuses to "restore" a lesson whose text has
been rewritten since, because flipping a status back would put a *different*
rule under the same name. Approving does not cut a release automatically:
six approvals over an afternoon are usually one deployment, and only you
know where that boundary is.

**Named environments track which release each one is running.**

```bash
commontrace release promote <release_id> stage
commontrace release promote <release_id> prod --at 2026-04-01T09:00:00Z  # scheduled
commontrace release current prod
commontrace release pending prod
```

`dev`/`stage`/`prod` are the closed set. `promote` needs no separate
activation step for a scheduled promotion — it carries the moment it
should take effect, and `current` simply starts returning it once that
moment arrives. Promoting into `prod` goes through the same
separation-of-duties check a single lesson's own approval already does
(above), checked only against lessons **newly** entering the
environment — one already running there isn't re-litigated on every
promotion.

**This is pure record-keeping, not a rollout mechanism.** Retrieval
still reads a lesson's own `status`, exactly as before `environments.py`
existed — there is no canary/ring *traffic* targeting between
environments, and none is planned as a quick follow-up: a release pins
a lesson to a content-addressed **hash**, not its actual text, and
nothing in this store retains what a lesson used to say once it's been
edited again (the same limit `release rollback` already lives with
honestly, by refusing rather than fabricating). Serving "what prod is
running" for an edited lesson needs a real revision *store*, which is
separate, larger work.

`distill` clusters traces by word-overlap similarity (pure Python, no LLM
call, no API key) and never writes anything above `status: review` — a
candidate only becomes retrieval-eligible once a human runs `lesson
approve`. Traces already referenced by an existing lesson's `source_traces`
are skipped on the next run, so re-running `distill` doesn't keep
re-proposing patterns someone already curated.

**A candidate arrives with its evidence, grouped.** Writing the rule is the
expensive step in this whole pipeline, so the proposal carries what you need
to write it — the situation *and* what actually resolved it, with repeats
collapsed and counted:

```markdown
## Why
12 traces show this pattern. Grouped, they say:

**The situation**
- (12 of 12) Customer scheduled a large CSV export and received a zero-byte file with no error in the UI.

**What worked**
- (12 of 12) The export exceeded the worker memory ceiling and was OOM-killed without surfacing. Re-ran with date chunking.
```

The case worth having is the other one. When a cluster has several distinct
resolutions, all of them are listed and the candidate is flagged:

> Note: 3 different resolutions for the same symptom. That usually means
> this is more than one problem — consider splitting the candidate, or
> narrowing `applies_when` until it covers only one.

One symptom with three root causes, written up as a single rule, produces a
lesson that fires on cases it cannot help. Nothing surfaced that before.

**`approve` refuses a lesson that is still template text.** A candidate
arrives with `applies_when`, the Rule, and the counter-examples all written
as `TODO: ...`; approving it as-is would activate a lesson that teaches the
fleet nothing, and an agent injects whatever it is given. Such a lesson
would also be counted as coverage by `commontrace taxonomy`/`pilot` (making
a real gap read as "Gaps: 0") and published to every agent by `sync --push`.
So `approve` names the unfilled sections and stops; `--force` overrides it
and says so; `lesson validate` fails an *active* lesson in that state; and
`sync --push` will not publish one. Fill it in first — that editing pass is
the curation step, not a formality.

### 6 — Measure what another fleet's lessons would be worth to you

```bash
commontrace overlap sign --fleet-label acme --out acme.json      # signatures, not content
commontrace overlap report --ours acme.json --theirs partner.json
```

Answers *"of the failures we keep hitting, how many has another fleet
already solved?"* for two fleets who have agreed to compare notes directly
— distinct from the CommonTrace Knowledge Base below, which has no
fleet-to-fleet data flow at all (see [`STRATEGY.md`](STRATEGY.md)).
Neither side sends the other any lesson or trace text; only MinHash
signatures are exchanged.

Lesson slugs and tags **do** travel by default so the report can name what
matched — a slug like `lesson_stripe_idempotency` describes itself. Add
`--redact-labels --redact-tags` to strip them; the measurement is
unchanged. This is not a cryptographic privacy guarantee — see
`commontrace/overlap.py` for exactly what it does and does not protect.

### 7 — Check whether your lessons actually work

```bash
commontrace reliability                 # score lessons against outcomes
commontrace reliability --strict        # non-zero exit if any lesson is HARMFUL
```

Joins *which lessons were injected* to *how the task turned out*, and
reports four verdicts: **RELIABLE**, **UNPROVEN** (not enough evidence yet
— stated rather than guessed), **MISCALIBRATED** (fires often, rarely helps
→ tighten `applies_when`), and **HARMFUL** (tasks go measurably worse when
it is injected → the rule may be wrong). Precision uses a Wilson lower
bound, so one lucky hit never outranks a long track record.

It also flags **contradictions** — pairs of `active` lessons that fire in
overlapping situations but pull in opposite directions, which an agent can
otherwise receive both of at once.

It also surfaces, separately from every verdict, occasions the holdout log
shows a lesson was actually injected into (a `--experiment` retrieval) that
no `capture` ever recorded an outcome for — an **"Under-reported"** section,
not a penalty: an unknown outcome is a different fact from a confirmed
miss, and folding it into precision would make an under-captured lesson
look worse than measured for no reason but under-reporting. It's a prompt
to capture more, not a number a verdict absorbs.

Nothing is changed automatically; the Validator gate stays human.

**`commontrace lesson suggest-revision <slug>`** goes one step further than
the label, for a MISCALIBRATED lesson specifically (fires often, rarely
helps — a narrowing problem, not necessarily a wrong rule): it drafts a new
`status: review` lesson from that lesson's own retrieval evidence — which
occasions it fired on, and which of those it actually helped — with
`applies_when`/`do_not_apply_when` marked for a human or agent to tighten.
Nothing about the original lesson changes until that draft is approved:

```bash
commontrace lesson suggest-revision my_broad_rule
# -> drafts lesson_my_broad_rule-revision (status: review), evidence attached
commontrace lesson approve my_broad_rule-revision   # once tightened
commontrace lesson reject my_broad_rule --reason "superseded by my_broad_rule-revision"
```

Refuses outright for any other verdict — a HARMFUL lesson's rule may be
wrong, not just its activation condition, and tightening WHEN it fires
would not fix that (the message points at `lesson reject` instead). This
does not call an LLM: nothing in this codebase's core pipeline does, and
this command aggregates real evidence into one place for a human or an
LLM-driving agent to act on, rather than adding a new dependency to draft
prose automatically.

**`commontrace consolidate`** asks the other question `reliability` doesn't:
not "does this lesson work", but "does the corpus have two lessons saying
the same thing". It reports fusion candidates (near-duplicate active
lessons, lexical — no `attention` extra required), lessons that have
`uses: 0` / `last_hit: NEVER` (injected whenever their condition matched and
never once counted as used — an archive candidate), and reuses
`reliability`'s own contradiction check, in one place:

```bash
commontrace consolidate            # fuse / archive / contradict, reporting only
commontrace consolidate --strict   # non-zero exit for a periodic hygiene check in CI
```

`commontrace lesson approve` already refuses to activate a lesson that
restates one already active (same near-duplicate check, same measured
threshold — see `commontrace/redundancy.py`), so a fresh duplicate cannot
enter the corpus unnoticed; `consolidate` is what finds the ones already
in it, and what an injection budget's own `--redundancy-threshold`
(`commontrace retrieval`) can drop from what an agent actually receives —
see `commontrace/dosage.py` for that opt-in.

### 8 — Retrieve a lesson for an incoming task

```bash
commontrace query "customer is escalating about a delayed refund"
commontrace query "..." --lexical        # force the dependency-free fallback
```

Falls back automatically to a pure-Python lexical (word-overlap) ranker if
the optional `attention` extra (semantic embeddings) isn't installed —
`commontrace query` always returns something with just the core install,
rather than failing outright.

Ranking can also weigh a lesson's own track record and freshness, not just
today's topical match — opt-in, and never a change to which lessons are
*eligible* at all (the relevance floor is unaffected either way):

```bash
commontrace retrieval --reliability-weight 0.2   # a HARMFUL/MISCALIBRATED verdict ranks lower
commontrace retrieval --recency-weight 0.2       # a lesson nobody has hit in a year ranks lower
```

Both default to 0 (off). See `commontrace/reliability.py`'s
`ranking_adjustments` and `commontrace/recency.py` for what feeds each one
— the former reads the same `reliability` verdicts, the latter the
existing `last_hit` field, no new schema required.

A long, multi-turn occasion that retrieves more than once can skip
guidance it has already been shown, so it isn't re-injected on every call:

```bash
commontrace query "..." --exclude-shown occ-4711
```

Reads the holdout log for lessons already logged as injected (not
withheld) under that `occasion_id` from a prior `--experiment` call; a
`core: true` lesson is never excluded (it's the fleet's unconditional
position, present every call by design). A prior call for that occasion
made without `--experiment` left no record, so nothing is excluded for
it — this is a real, stated limitation, not a silent gap. MCP's
`retrieve` takes the identical `exclude_shown` parameter.

**Using CommonTrace as a library, not just a CLI:** a second-stage
reranker (a cross-encoder, an LLM judge, a bespoke scorer) has no seam to
plug into via a CLI flag — it's code, not a config value — so this is a
composition point for a Python caller instead:

```python
from commontrace import lesson_cache, retrieval

lessons, term_cache = lesson_cache.load_active_with_terms(root, agent_type=None, reader=...)
ranked = retrieval.rank_lessons(task, lessons, term_cache=term_cache)
ranked = retrieval.apply_reranker(task, ranked, my_reranker)  # my_reranker(task, candidates) -> candidates
```

`apply_reranker(task, ranked, None)` (the default) is a no-op. This is the
same shape `commontrace/redundancy.py`'s own `similarity` parameter
already uses — a plain callable, not a class hierarchy, so the core
install never gains a hard dependency for an extension point most stores
never use.

### 9 — Prove the lessons *cause* the improvement

```bash
commontrace query "..." --experiment --occasion-id task-4711   # withhold at random, log the arm
# ...do the task...
commontrace capture --title "..." --context "..." --solution "..." \
    --agent-type code --occasion-id task-4711 --resolved        # same id: this is the join
commontrace experiment --plan --occasions 500   # design it FIRST: what rate answers this?
commontrace experiment                  # causal effect per lesson, validity checked first
commontrace experiment --strict         # non-zero exit if a lesson HURTS, or if the run is not valid
```

**Step-by-step, with the sample sizes you need: [PILOT.md](PILOT.md).**

Every other number in this repo — including `reliability`'s `lift` — is
**correlational**, and the confound is structural: a lesson is retrieved
*because* the situation matched its activation condition, so the occasions
where it fired differ systematically from the ones where it didn't. A lesson
that fires on routine work looks brilliant while contributing nothing; one
that fires only on the gnarliest incidents looks harmful while being the
reason those incidents got resolved. That bias does not shrink with more
data.

`--experiment` withholds a lesson from a random ~10% of the occasions where
it *was* eligible, and logs which arm each occasion landed in. Comparing
those two arms is a controlled experiment on your own fleet, so the result
is causal: **"tasks resolved 34% more often with this lesson injected (95%
CI [12%, 56%], p = 0.002)"**.

- Assignment is a deterministic hash of `(lesson, occasion, salt)` — no
  stored state, exactly reproducible months later when someone disputes a
  result, and stable under retries, so an occasion can't flip arms by being
  processed twice.
- It is **independent per lesson**, which is what makes two lessons that
  always fire together separable at all. No observational method can do
  that: they share every outcome.
- Verdicts are **HELPS**, **HURTS**, **NO_MEASURABLE_EFFECT**, and
  **UNDERPOWERED** — the last stated separately so "not enough data yet" is
  never read as "tested and found useless". Significance is
  Benjamini-Hochberg-corrected across all tested lessons, because at
  α = 0.05 over 100 lessons ~5 look significant by chance and those are
  exactly the ones that get quoted.
- Every look is treated as a look at a **running** experiment. A verdict
  must clear a confidence interval that holds at every sample size, because
  a fixed 5% threshold checked on every CI build, or every time someone
  opens the report, is eventually crossed by luck. `commontrace
  experiment`, `--strict`, `pilot`, the MCP tools and the Hub all read it
  this way, so they give the same verdict. `--fixed-horizon` is the
  one-shot reading, for a finished run that nobody acted on midway.
- "No measurable effect" is always reported alongside the **minimum
  detectable effect** for that sample, so it reads as a statement about the
  experiment's power rather than about the lesson.

The cost is bounded and explicit: in the worst case the lesson would have
helped, and 1 occasion in 10 loses that help. That is the price of knowing.

Add `--resolved`/`--not-resolved`, `--escalated`/`--not-escalated`,
`--repeated-error`/`--not-repeated-error`, `--frustration`/`--not-frustration`,
`--tokens-used N`, `--llm-calls N`, and `--baseline` to `capture` to record the
outcome data behind the outcome metrics (§ [Outcome Metrics](#outcome-metrics)
below) — all optional, all additive to the base capture.

#### Already have a memory store? Measure it without migrating

The same experiment runs on memories held by any other system — a vector
database, a memory SDK, a LangGraph store, your own table. Wrap the
retrieval call you already make; nothing is copied into CommonTrace and
nothing is written back to your store.

```python
from commontrace.measure import CausalMemory

memory = CausalMemory(my_store.search)   # any callable returning ranked items

items = memory.recall(task.query, occasion_id=task.id)   # extra kwargs pass through
# ...run the task with `items`...
memory.record_outcome(task.id, succeeded=task.passed)
```

Then `commontrace experiment` in the same store reports a verdict per
memory, with the same validity checks as above.

- **Ids.** Each item needs a stable id — an `id` or `key` field or
  attribute, or pass `key=`. There is deliberately no fallback to
  `str(item)`: that usually embeds a memory address that changes between
  processes, which would scramble the arms without raising anything.
- **Edited memories.** Stores that update a memory in place keep its id
  while its text changes. The item's text (`memory`, `text`, `content` or
  `value`, or pass `text=`) is fingerprinted so the two versions are not
  measured as one.
- **Pinned memories.** Pass `pinned=[...]` for ids that must always be
  delivered; they are never withheld and never enter the experiment.
- **Outcomes.** Reporting the same outcome twice is harmless; reporting a
  different one for an occasion already on record raises rather than
  silently changing the result.

#### When a lesson is proven to hurt: stop handing it out

A HURTS verdict is only useful if something happens next. Opt in, and
retrieval stops injecting a lesson the experiment has shown makes outcomes
worse:

```bash
commontrace retrieval --on-harm withdraw     # default: inform
```

The lesson is not hidden. Every retrieval it would have appeared in names it
under `withdrawn` (MCP `retrieve`) or a `withdrawn --` line (`commontrace
query`), with its effect and interval, and the retrieval receipt records why.
Its slot goes to the next-ranked lesson.

This doesn't bias the experiment that produced the verdict:

- It acts only on the **anytime-valid** verdict (a confidence sequence that
  holds at every sample size). Stopping when a boundary is crossed is the
  one look a fixed 5% threshold can't survive. A modest harm that a fixed
  test would already call HURTS stays in service until the sequential
  bound agrees.
- It is decided **before arms are assigned**, so a withdrawn lesson is never
  logged as treated or withheld on an occasion it was absent from.
- Ranking runs with the lesson still present, and the lesson is removed
  afterwards, so every other lesson's relevance, and therefore its
  eligibility, is unchanged.
- Nothing happens while the experiment's audit is COMPROMISED. A new
  randomization (`commontrace experiment --configure`) starts every lesson
  with no verdict, which is how a rewritten lesson gets a second trial.
- `core: true` lessons are exempt: they are never randomized, and core
  means "present every time".

On the Hub, `python -m hub.manage harm-policy <org_id> withdraw` does the
same for `search_traces`. The trace's near-duplicates are withdrawn with it,
because the holdout randomizes a duplicate cluster as one unit, so the
verdict belongs to the whole cluster. Amendments are not: an amendment has
its own id, is usually the fix, and gets its own trial.

### 10 — Run the pilot as one command: map, measure, and a yes/no

```bash
commontrace taxonomy      # "Map the issues": a structured map of the failure patterns found
commontrace impact        # Impact Dashboard: errors avoided, lessons reused, value generated/saved
commontrace pilot         # all of the above + baseline-vs-current resolution rate + a yes/no gate
commontrace pilot --html  # a self-contained report written to memory/benchmark_reports/
```

`taxonomy` reuses `distill`'s clustering but never writes anything and does
not exclude traces an existing lesson already covers — it shows the whole
map, marking each recurring pattern **covered** (an active lesson references
it) or a **gap**. `impact` counts errors avoided and lessons reused directly
from the same retrieval evidence `reliability` reads (correlational, exactly
like `reliability`'s `lift`); a dollar total is only ever computed from a
`--cost-per-1k-tokens`/`--value-per-error-avoided` rate you supply — omit
both and it reports the measured counts with no dollar figure attached,
matching [the Plans section below](#plans-and-what-they-actually-enforce)'s
"no currency appears anywhere in this repository." `pilot` bundles both plus
`bench --pilot`'s resolution-rate delta into one report, and its yes/no gate
is deliberately conservative: a causal result from `commontrace experiment`
(§9 above) always outranks a correlational one, and correlational data alone
never earns an outright yes — see PILOT.md.

See [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md) for the object model these
commands produce, and `commontrace --help` / `commontrace <subcommand> --help`
for the full CLI reference.

---

## Quick Start — Agents with no terminal (MCP)

Everything above is a CLI, which quietly restricts CommonTrace to agents that
can run a shell. Most cannot: a support agent inside a helpdesk, a sales agent
inside a CRM, an ops agent inside a runbook tool. The Hub has spoken MCP since
it existed; the **local store now does too**.

```bash
commontrace install --target claude-code   # writes commontrace.local.mcp.json
```

That file is the MCP entry — merge it into your agent platform's config:

```json
{
  "mcpServers": {
    "commontrace-local": {
      "command": "commontrace",
      "args": ["serve", "--dest", "/abs/path/to/your/store"]
    }
  }
}
```

The agent then has the whole protocol as tools:

| Tool | What the agent does with it |
| --- | --- |
| `retrieve(task, occasion_id?)` | Find the lessons that apply, before acting. With an `occasion_id`, applies the randomized holdout and returns what to *not* use under `withheld`. Also returns `budget` (how much of the context allowance this retrieval spent), `core` (always-on lessons, injected whether or not they matched), and `not_injected` — what did not fit, named rather than silently dropped. |
| `capture(...)` | Record what happened, with the outcome fields (`resolved`, `tokens_used`, …). Same `occasion_id` joins it back to the retrieval. |
| `propose_lessons()` | Cluster repeated failures into candidates. |
| `draft_lesson(slug, rule, why, …)` | Write a candidate's content, over as many calls as it takes. |
| `approve_lesson(slug)` | Activate it, so retrieval starts injecting it. |
| `reject_lesson(slug, reason)` | Archive one that should not become a lesson. |
| `list_lessons` / `get_lesson` / `store_status` | Read the store, and see which recurring patterns still have no lesson. |
| `experiment_status()` | Is the randomized holdout you're feeding with `occasion_id` actually going to answer anything yet -- validity, power projections, and effects so far, so an agent can tell a running pilot from a spent one. |

Two things about this are deliberate.

**There is no authentication, because there is no boundary to authenticate.**
The client spawns this process and talks to it over its own stdin/stdout —
no port, no listener, nothing for another program on the machine to connect
to. It reads and writes `memory/` with exactly the permissions of the agent
that launched it, which already had them. That is the opposite of the Hub,
which is multi-tenant and network-reachable and therefore authenticated on
every call.

**An agent can approve its own lesson by default, and the store can forbid
it.** `approve_lesson` refuses a lesson that still contains scaffolding (an
active lesson is injected into every later retrieval *verbatim*, so a rule
still reading `TODO:` teaches the fleet nothing and displaces a real one),
refuses one carrying a secret or a prompt-injection payload
(`commontrace/memory_guard.py`), and records **who** approved it, so an
agent-approved lesson stays distinguishable from a human-approved one.

Those all check *what* is being activated. For *who*, write
`memory/approval-policy.yaml`:

```yaml
mode: two-person      # the approver must not be among the lesson's recorded authors
require_human: true   # an `mcp:` actor's approval does not satisfy the gate at all
```

With no such file the behaviour is unchanged — anyone may approve, including
the author — so an existing store sees nothing new until it opts in.
Authorship comes from the revision journal every content change already
writes, and `--force` does not override it: that flag exists for an author
who has judged a content warning a false positive, which is exactly the
judgement a separation-of-duties policy says this person may not make. Where
a person must be in the loop entirely, `commontrace serve --no-approval`
removes the tool — absent from the listing, not present and refusing, so the
agent never plans around a call it cannot make.

None of this authenticates anybody: the local tier has no identity system,
so an actor string is an attribution, not a proof. What the policy changes
is the default path — the ordinary way to approve your own lesson stops
silently working, which is what a control is for.

Nothing here reimplements ranking, holdout assignment, or the approval guard —
it calls the same functions `commontrace query` and `commontrace lesson
approve` do, so a fleet's shell-capable and shell-less agents read the same
memory and land in the same experiment arms.

---

## Quick Start — Code Agent reference profile

### 1 — Install

```bash
git clone https://github.com/varma61923/commontrace-v2 commontrace
cd commontrace
./install.sh                        # installs to ~/.commontrace (default)
# or
./install.sh --dest /my/custom/path
# or
./install.sh --in-place             # use the repo checkout directly (no copy)
```

### 2 — Invoke

Point your agent at `SKILL.md` and invoke with:
```
/commontrace <task description + success criteria>
```

Example:
```
/commontrace Refactor the scan module to use vectorized ops.
Success: all existing tests pass, no Python loops where numpy is applicable.
```

The pipeline is platform-agnostic. See `SKILL.md` > "Platform Mapping" for how
generic concepts map to your specific agent platform.

### 3 — Run the benchmark

```bash
# Markdown summary to stdout (scripts auto-detect their root, or: commontrace bench)
commontrace bench

# Last 5 episodes only
commontrace bench --n 5

# HTML report
commontrace bench --html
```

---

## How It Works

```
Phase 0  → Alpha   reads memory/, surfaces relevant past lessons
Phase 1  → Parse task, ask for clarification if needed
Phase 2  → Create task entry
Phase 3  → Agent A  implements
Phase 4  → Commit immediately after A
Phase 5  → Agent B  reviews independently (no knowledge of A's choices)
Phase 6  → Decision
           ├─ CONFORM  → done
           └─ GAP
               ├─ iter < max(3) → Phase 7: re-brief A with B's gaps
               └─ iter ≥ max    → Phase 8: orchestrator arbitration
Phase 9  → Orchestrator mini-retro (feeds Omega)
Phase 10 → Omega   writes episode, proposes new lessons
Phase 11 → Lambda  validates lesson proposals automatically
```

**When to use** — Architectural refactors, heavy rewrites, CUDA/GPU ports, critical bug fixes,
any task where independent review adds real value.

**When NOT to use** — Trivial changes (< 20 lines, < 3 files). Handle those directly in the
main session for speed.

---

## Architecture

| Agent | Role | Phase |
|---|---|---|
| **Alpha** | Retrieves relevant past lessons from memory | 0 |
| **A** (Implementer) | Writes the code | 3, 7 |
| **B** (Reviewer) | Audits independently against success criteria | 5 |
| **Omega** | Writes the run episode; proposes lesson candidates | 10 |
| **Lambda** | Validates lesson proposals (auto, no human needed) | 11 |
| **Orchestrator** | Coordinates the pipeline, takes decisions at phase 6/8 | all |

Plus an **attention layer** — a local numpy/sentence-transformers embedding index that
pre-filters lessons for Alpha at scale (100+ lessons without latency degradation). The
scripts ship inside the package (`commontrace/reference/`) and are driven by `commontrace
query` / `commontrace index`; each store keeps its own generated index at
`memory/attention/index.npz`.

Architecture diagrams: `assets/commontrace_overall.png`, `assets/agent_*.png`.

---

## Configuration

### Path resolution

All Python scripts **auto-detect their root** from their own file location. If you
clone the repo and run scripts from within it, everything works out of the box.

### Environment variable (optional override)

| Variable | Default | Description |
|---|---|---|
| `COMMONTRACE_ROOT` | Auto-detected from script location | Root of the skill installation |

Only set `COMMONTRACE_ROOT` if you need to override the auto-detected path (e.g., scripts
are running from a different location than the memory store).

```bash
export COMMONTRACE_ROOT=/opt/commontrace
commontrace query "my task"
commontrace bench
```

---

## Memory System

Long-term memory is stored under `memory/`:

```
memory/
  INDEX.md              — hierarchical index by domain (edited by orchestrator Phase 11)
  lessons/              — one .md file per validated procedural rule
  episodes/             — one .md file per /commontrace run (written by Omega)
  attention/
    index.npz           — generated embedding index (gitignored; rebuild with `commontrace index`)
```

The scripts that build and read that index (`build_index.py`, `query.py`) live in
`commontrace/reference/` and ship with the package, so semantic retrieval works from a
plain `pip install` rather than only from a repo checkout.

**Memory starts empty.** The example entries in `memory/lessons/` and `memory/episodes/`
are illustrative templates — delete them once your own runs accumulate.

**Rebuild the attention index** after adding or editing lessons:

```bash
commontrace index
commontrace index --force      # rebuild even if it looks current
```

Nothing rebuilds it automatically (it loads a ~420 MB model and can hit the network), so
`commontrace query` checks freshness cheaply and **falls back to lexical retrieval** when
the index is missing or stale, rather than returning nothing. Lexical reads the lesson
files as they are and cannot go stale — it is also what the MCP server always uses.

The embedding model is downloaded once and cached under `~/.cache/huggingface/`.
A new index uses `Snowflake/snowflake-arctic-embed-m-v1.5` (~440 MB); an index
built before it keeps `multi-qa-mpnet-base-dot-v1` until you rebuild it with
another (`commontrace index --force --model <name>`), because the model decides
which lessons the semantic arm finds, and an experiment records it as part of
the treatment. On LoCoMo, arctic-embed's exact cosine search finds an answering
turn in the top 10 for 70.6% of questions, against 56.1% for mpnet.

---

## Benchmark

`commontrace bench` measures memory health across three axes:

| Metric | What it measures |
|---|---|
| `lesson_quality` | % of Omega proposals accepted by Lambda |
| `implicit_retrieval` | % of Alpha-retrieved lessons that actually helped |
| `transfer_gap` | % of hits that crossed project boundaries |

Run periodically (every 5-10 `/commontrace` runs). See `benchmark/STATUS.md` for
interpretation guidelines and the roadmap.

`measure_performance.py` answers "is the protocol machinery healthy" — is
generation (Omega) and retrieval (Alpha) doing its job. It does **not**
answer "did the fleet's behavior actually change," which is a different,
business-facing question — see the next section.

---

## Outcome Metrics

`commontrace bench --pilot` computes
five business-outcome metrics from `Trace.outcome` data
(see `protocol/schemas/trace.schema.json` and
[`protocol/PROTOCOL.md`](protocol/PROTOCOL.md#11-pilot-outcome-metrics)):

| Metric | What it answers |
|---|---|
| Repeated-error rate | Is the fleet still making mistakes it's already seen? |
| Resolution rate | Are more tasks reaching a successful conclusion? |
| Escalation rate | Are fewer tasks needing a human? |
| Frustration rate | Are fewer users/customers unhappy with the outcome? |
| Token / LLM-call cost | Is lesson injection making runs cheaper, not just better? |

```bash
commontrace capture --title "..." --context "..." --solution "..." \
  --resolved --tokens-used 420 --llm-calls 3 --baseline   # pre-CommonTrace baseline
commontrace capture --title "..." --context "..." --solution "..." \
  --resolved --tokens-used 300 --llm-calls 2               # post-injection (default)

commontrace bench --pilot              # markdown: baseline vs. current, all 5 metrics
commontrace bench --pilot --json       # machine-readable
commontrace bench --pilot --agent-type support   # filter to one fleet
```

Traces marked `--baseline` are compared against everything else, giving a
before/after change per metric — computed from your own fleet's traces.
Any `outcome` field left unset is simply excluded from its metric rather than
counted against you, so you can adopt outcome tracking incrementally.

This is a standing production measurement, not a one-time evaluation. The
same baseline/current split that answers "did adopting this help?" in the
first month answers "is it still helping?" a year in, and regressions show
up as a metric moving the wrong way. For a *causal* rather than
correlational answer on a specific lesson, see `commontrace experiment`
(randomized holdout, § [Benchmark](#benchmark)).

### Is retrieval as good in *your* field as in ours?

`commontrace bench --pilot` measures your fleet. `commontrace bench --retrieval`
measures the retriever itself, **per field**, against a labelled corpus that ships
with the package (eight fields, 48 lessons, 144 queries):

```bash
commontrace bench --retrieval                                  # per-field table
commontrace bench --retrieval --json                           # machine-readable
commontrace bench --retrieval --max-pollution 1.5 --max-spread 2   # CI gate
```

It exists because a single aggregate number cannot show the failure it is
looking for. Scoring used to be raw word overlap, which rewards whichever
field writes more — so a threshold meant something different in a terse coding
store than in a wordy legal one, and nothing would have caught a change that
improved coding at legal's expense.

The headline metric is **pollution**: assignments logged per assignment
actually about the lesson. Under `query --experiment` every retrieved lesson
is logged as an eligible holdout assignment, so a lesson retrieved into tasks
it has nothing to do with absorbs those tasks' outcomes — which is how one
lesson accrued 246 assignments against ~80 real occasions and was reported as
significantly *hurting* outcomes when it was fine.

The gate is deliberately two-sided (a ceiling on the worst field **and** the
worst÷best spread): the historical scorer polluted at 1.89×–2.50× while its
*spread* was 1.32×, so a spread-only gate would have called it acceptable.
Methodology, thresholds and limitations: [`benchmark/STATUS.md`](benchmark/STATUS.md) §9.

---

## How much memory, and what was actually there

Ranking answers *which* lessons match. Two other questions decide what an
agent actually receives, and both used to go unanswered.

**Which retriever.** Until recently `commontrace query` picked *one*:
semantic when the attention extra was installed and the index was fresh,
lexical otherwise. Whichever it picked, the other arm's signal was thrown
away — so a store with the extra could not find a lesson whose exact error
string you had pasted in, and a store without it could not find one phrased
differently from the task. They fail on different queries, which is exactly
when fusing beats picking:

```bash
commontrace retrieval --fusion rrf
```

Fusion is by **rank, not score**: the lexical arm returns an IDF relevance in
[0,1] and the semantic arm a cosine similarity, and there is no honest
conversion between them. A lesson only one arm surfaced is not penalised for
the other's silence.

Agents get it too. The MCP `retrieve` tool runs the same semantic ranking
in-process (`commontrace/semantic_arm.py`), with the model and index held in
memory rather than reloaded on every call. It uses the same freshness gate
and fallback as `query`, so an agent and a person at a terminal get the same
fused ranking and log the same eligibility label.

The semantic index keeps itself current. When a lesson has been added or
edited since the last build, both surfaces refresh the index before ranking,
re-embedding only the lessons whose text changed. Before, the store fell back
to keyword-only retrieval until someone ran `commontrace index`. The first
fused retrieval on a store with no index embeds every lesson once, which costs
the same as running `commontrace index`.

It is opt-in for a reason. Fusion changes which lessons are *eligible*, and
eligibility is the denominator of every causal number this product reports —
so the arm composition is recorded inside the label each holdout assignment
carries (`rrf(idf-v2+semantic)`), and turning it on mid-experiment is
reported as a compromised run rather than absorbed silently.

**Reranking.** Both arms score the task and a lesson separately. A
cross-encoder reads them together, which ranks far better and is far too
slow to run over a whole store, so it runs over the first stage's top 30
and only reorders them:

```bash
commontrace retrieval --fusion rrf --rerank cross-encoder        # most accurate
commontrace retrieval --fusion rrf --rerank cross-encoder-fast   # ~10x faster
```

**Gated fusion, on by default.** Plain fusion fills every slot on the page
with whatever the semantic arm ranked, so a curated store under experiment
logs lessons the task was not about. Gated fusion (`--fusion gated`) feeds
both arms to the reranker but lets a lesson that did not clear the relevance
floor onto the page only when the cross-encoder vouches for it. On the
curated fixture every field keeps exactly its recall and collateral; on
LoCoMo, with the arctic-embed semantic arm a new index uses, it reaches R@5
60.8% and R@10 69.4% with the fast reranker and 70.3% / 76.2% with the
accurate one.

A store that has not chosen, and has no experiment history, gets gated
fusion with `cross-encoder-fast` wherever the attention extra is installed
(lexical retrieval with the fast reranker if the semantic arm is turned
off). The first retrieval embeds the store's lessons; later ones re-embed
only what changed. A store mid-experiment stays on what its log says it
ran. `COMMONTRACE_DEFAULT_FUSION=none` keeps the reranker without the
semantic index, and `COMMONTRACE_DEFAULT_RERANK=none` turns both off.

On LoCoMo, fused retrieval with reranking puts an answering turn in the top
5 for 67.0% of questions, up from 53.1% without it.
`cross-encoder` (`ms-marco-MiniLM-L-6-v2`, 22M parameters) reranks 30
candidates in about 265 ms on a 4-core CPU; `cross-encoder-fast`
(`ms-marco-TinyBERT-L-2-v2`, 4M) in about 30 ms, reaching 60.6%. Both come
with the attention extra and download on first use. Like fusion, it
decides which lessons make the page, so assignments record it
(`ce:minilm6(rrf(idf-v2+semantic))`) and turning it on starts a new
treatment. A store that cannot load the model ranks exactly as if it had
not asked, and says so.

**Stemming.** `commontrace retrieval --scorer idf-v3` makes "retrying" match
"retry" and "uploads" match "upload". It lifts recall on free-text and
conversational memory (LongMemEval session recall@5 0.852 → 0.926). It is
not the default, because in one curated field of the fixture (clinical) it
retrieves more collateral than `idf-v2`. It has its own floor, which the
store takes automatically when switching, and like any eligibility change
it starts a new randomization.

**How much.** `top_k` bounds the count and says nothing about the size — ten
terse lessons and ten pages of prose are the same `top_k=10`, and the second
one displaces the task itself out of the context window. Retrieval admits
against a budget in characters as well as count:

```bash
commontrace retrieval --max-lessons 10 --max-chars 8000
commontrace retrieval          # show what this store is set to
```

Every `retrieve` reports what it spent (`"budget": "4/10 lessons, 3,140/8,000
chars (39%)"`) and names what did not fit under `not_injected`, with the
reason. Nothing is silently truncated: an agent given nine of ten lessons and
told it was given ten will act on the missing one's absence as though it were
the fleet's position.

**Which is unconditional.** Some rules are not "relevant to this task" — they
are how the fleet operates. Mark one `core: true` in its frontmatter and it is
admitted ahead of the matched set, every time:

```yaml
name: always-use-idempotency-keys
core: true
importance: 5
```

Before this, the only way to make a rule reliable was to make it match
everything, which is the same thing as making retrieval worse. Core lessons
are still budgeted — they compete only with each other, by importance —
because a fleet that marks forty lessons core has not thereby earned forty
lessons' worth of context, and the unconditional reading's failure mode is
that the always-on set crowds out every matched lesson and retrieval appears
to stop working, with no error.

**What was actually there.** Each retrieval with an `occasion_id` writes a
*receipt* (`memory/retrieval_receipts.jsonl`): the candidate set that was
**visible** — every active lesson, pinned to its revision, with a digest over
the set — the subset **admitted**, and, written later as its own line, which
of those the agent says it **used**.

The holdout log records eligibility and arm, which starts one step too late. A
lesson that was never a candidate does not appear in it at all, so *"the
memory did not help"* and *"the memory was never offered"* are
indistinguishable afterwards — and they have opposite remedies. The
injected-versus-used split is the other thing receipts make possible: every
"lessons reused" figure before them was counting injections.

Because receipts store revisions rather than names, a dispute six months later
about what the agent had in front of it is answerable to the exact text, not
to a slug whose contents have moved since.

---

## The CommonTrace Knowledge Base

Everything above is the on-prem, self-learning fleet: a fleet's own
experience compounds *for that fleet*, on its own infrastructure, and no
other customer ever reads it. The Knowledge Base is a separate, optional
layer next to it — closer to a vendor-maintained Stack Overflow or wiki
than to a shared corpus between customers.

There is no org-to-org sharing anywhere in this system, and that is a
deliberate, load-bearing property, not an oversight:

> **No customer's own trace is ever visible to another customer, and no
> customer's trace ever enters the Knowledge Base as itself — only as
> content an operator has explicitly reviewed and republished.**

The Knowledge Base is a single corpus the *operator* authors and curates —
substrate knowledge ("Stripe webhook handlers need idempotency keys",
"React 19 hydrates `Date` differently than 18") that isn't anyone's trade
secret, shipped and maintained the way a vendor maintains documentation.
`hub/manage.py commons-seed` (bulk load) and `approve-submission` (one
community proposal at a time — see "Propose an entry" below) are the only
two things that ever write to it, both operator-run; no customer-facing
tool can. `commons_access` (a plan setting) makes consulting it optional
per org, and `HUB_COMMONS_ENABLED=false` removes it from the deployment
entirely — see [`hub/DEPLOYMENT.md`](hub/DEPLOYMENT.md) for the single-org
deployment mode that needs neither.

An earlier design routed this as org-to-org sharing instead (customer A
opts a trace in, customer B's queries can match it). That design is
retired: it has an adverse-selection problem with no fix (why would an org
contribute knowledge that might help a competitor?), and it doesn't make
sense in the first place — orgs do not share their IP and data with each
other, so a design that asked them to was solving the wrong problem.

### Ask the Knowledge Base what it already knows

```bash
commontrace commons ask "customer charged twice for one order"
```

```
## 1. Payment webhook delivered more than once
*similarity 0.125 · trust 0.50 · code*

**When it happens:** The payment provider re-delivers a webhook after a non-2xx
or a timeout, so the handler runs twice and the customer is charged twice

**Solution:** Persist the provider's event id and check it before any side
effect. Make the handler idempotent at the write, not at the entry point.
```

This is the lookup — *"has anyone already solved this?"* — and it is a
different question from the coverage percentage below, with a different
answer shape and a different trade.

Note the similarity on that result: **0.125, well under the 0.30 coverage
threshold.** `commons report` scores that same failure as
**uncovered**, because a number you quote to a customer must not
over-claim. The knowledge was there the whole time; the meter was built to
say no when unsure. Ranking the same signatures instead of thresholding
them recovers it:

| | Recall@1 | @5 | @10 | Failure text sent? |
|---|---|---|---|---|
| `commons report` (thresholded) | 10.9% | — | — | No |
| **`commons ask` (ranked)** | **89.1%** | **95.7%** | **100%** | **No** |

Measured on 46 held-out failures written in on-call vocabulary
(`python commons/eval/search_modes.py`, recorded in
[`commons/eval/RESULTS.md`](commons/eval/RESULTS.md)). Your question is
MinHashed locally exactly as `sign` does it — **no failure text leaves your
machine for either command.**

**Results are candidates to judge, never coverage.** On the same
evaluation, a failure the Knowledge Base does *not* contain still comes back with
a non-empty list 100% of the time, and the true/absent score distributions
overlap. That is fine for a ranked list someone skims — a weak match costs
a glance — and it is exactly why the coverage figure keeps its threshold
and stays a separate command. Do not derive a percentage from `ask`.

### Ask what you'd gain, before adopting anything

```bash
commontrace commons sign --out failures.json      # local; signatures only
commontrace commons report --signatures failures.json
```

```
**2 of 3** of your recurring failures (67%) are already solved in the CommonTrace Knowledge Base.

### Postgres connection pool exhausted under retry storm
- Matches your `c9afa48d-761` at similarity 0.6406
**Solution:** Bound retries with jittered backoff and set a pool_timeout so callers fail fast.
```

`sign` MinHashes your recurring failures locally — **no failure text leaves
your machine**, and text cannot be reconstructed from a signature. What
comes back is drawn only from the operator-curated Knowledge Base — never
from another customer's own traces, because no customer's trace is ever in
that corpus. There is nothing to contribute in order to ask.

Stated plainly, because it matters: MinHash is not a cryptographic privacy
guarantee. Someone who can already guess a candidate string can test
whether it is present. Private set intersection is the real fix for
mutually distrustful parties and is a known follow-up, not a quiet
assumption.

### Evaluate before adopting anything

The Knowledge Base thesis is one empirical claim — that a meaningful share of what
your fleet keeps hitting is *substrate* failure someone else already
solved. Testing it should not require adopting CommonTrace first, so it
doesn't:

```bash
commontrace commons report --from incidents.csv    # or .jsonl / .json / .txt
```

Point it at data you already have — a ticket export, a postmortem index, a
pasted column of alert titles. It reads JSONL, JSON (bare array or the
usual `{"issues": [...]}` envelope), CSV/TSV with whatever your tool named
the columns, or one failure per line. No `commontrace init`, no captured
traces, no store on disk.

Signing happens locally and **failure text never leaves your machine** —
the same `overlap.minhash` the Hub uses, so an imported signature and a
stored trace's signature are the same object. Your incident *titles* do
travel, as labels echoed back in the report; the command says so before you
send anything, because for an imported file those titles are yours rather
than opaque ids. Exact duplicates are collapsed and the count reported —
otherwise one noisy alert repeated 400 times would dominate the percentage
and the number would describe that alert instead of your fleet.

**Read the result against [the measured recall](#measuring-coverage-honestly).**
A low number here is weak evidence: the matcher is lexical and misses most
failures worded differently from the corpus. A *high* number is strong
evidence, since false positives measured 0%.

### What is guaranteed

| Property | How |
|---|---|
| No customer trace ever enters the Knowledge Base without an operator's own decision | Only `hub/manage.py commons-seed` (bulk) and `approve-submission` (one community submission at a time) write `commons_source='seed'` rows — both operator-run, neither reachable from a customer's own API key |
| The guarantee holds even against a hypothetical bug | `commons_overlap`/`commons_search` filter on `commons_source == "seed"` explicitly, not merely on the absence of a sharing tool |
| Proposing is not publishing | `submit_kb_entry` writes to a separate table no commons query ever reads; it stays there, invisible to every other org, unless an operator's `approve-submission` accepts it |
| Quarantined content can't enter | Refused at seed time — that would propagate exactly what quarantine contains |
| Your own traces don't inflate your coverage number | The corpus excludes your rows: the question is what the Knowledge Base already knows, not what you told it |
| Ordinary reads are unaffected | All six org-scoped tools stay org-scoped; `hub/tests/test_tenant_isolation.py` passes unchanged |
| Consulting it is optional | `commons_access` (plan setting) per org, `HUB_COMMONS_ENABLED=false` for the whole deployment |
| No number of downvotes can delete the operator's content | Voting changes an entry's `standing`, which shrinks this product's own claims (dropped from coverage, ranked last) and never removes anything. Withdrawal is an operator action — `hub/tests/test_kb_standing.py:TestVotesNeverRetract` |

### Propose an entry

```bash
commontrace commons submit \
  --title "Postgres connection pool exhausted under retry storm" \
  --context "a dependency outage triggers a retry storm that saturates the pool" \
  --solution "bound retries with jittered backoff; set pool_timeout so callers fail fast" \
  --tags postgres,retries \
  --rationale "substrate connection-pool behavior, not our business logic"
commontrace commons submissions               # check status
```

This is closer to posting a Stack Overflow answer than to sharing your own
incident history: write it up as generalized substrate knowledge, not as
your specific outage. **Nothing is published by `submit`.** It creates a
row an operator reviews later (`hub/manage.py list-submissions` /
`approve-submission` / `reject-submission`); until decided, it is invisible
to every other org, including your own coverage numbers.

An **accepted** submission becomes a normal Knowledge Base entry — owned
by the operator, not by you, exactly like a seeded one — and permanently
raises your org's Knowledge Base query allowance by
`plans.SUBMISSION_ACCEPTANCE_CREDIT` (25 by default; an operator can grant
a different amount per submission). A **rejected or still-pending** one
earns nothing.

That "earns nothing until accepted" rule is the entire fix for the problem
an earlier, retired design had (§3 in `STRATEGY.md`): a credit for the act
of *sharing* rewards volume, and an org keeps its best lessons while
farming credit with filler. A credit for *acceptance* rewards quality
instead, because filler gets rejected. It does not make the underlying
incentive to withhold your best material disappear — nothing could — but
what accumulates is self-selected for being worth a human's time to
publish, the same as an actual Stack Overflow answer or wiki edit.

### Is the Knowledge Base actually earning its query traffic?

The operator-curated model has its own failure mode: a corpus nobody wrote
carefully fills with entries that never match anything real. So content
quality is measured, not assumed — every time an entry covers a real
recurring failure, that entry's `commons_hits` increments:

```bash
python -m hub.manage kb-stats    # entry count, hits, adoption, dead entries,
                                  # standing breakdown, and the submission
                                  # funnel: pending/approved/rejected
```

`kb-stats` is a content-quality report, not a vanity metric: it flags
entries that have never matched anything after real query volume (the
honest signal that they need rewriting or removal) and reports the
submission funnel so an operator can tell whether the review queue itself
needs attention, separately from whether its output is any good.

### Is it actually working for you?

Every trace you capture can carry an `outcome` — was the task resolved, did
it escalate, was it a repeat of a failure you'd already hit, what did it
cost — and `outcome.baseline: true` marks traces from a window *before*
lessons were being injected. The Hub compares the two:

```bash
# via the MCP tool your agents already have
fleet_outcomes()                          # optionally: agent_type="support"
```

You get resolution, repeated-error, escalation and frustration rates for
both windows with the delta, a 95% confidence interval, a p-value, and a
Benjamini-Hochberg correction across the four metrics. Reads only your own
traces, and is not metered.

**It is an observed change, not a causal effect, and it says so on every
response.** `baseline` is a time window, so a model upgrade or a shift in
your task mix is mixed in with anything CommonTrace contributed. For a
claim that survives "what else changed that quarter?", run the randomized
holdout (`commontrace experiment`) — it withholds lessons at random, so
the arms differ only by the treatment.

Three things it deliberately will not do for you:

- **Quote whichever of four metrics happened to look good.** The
  correction is applied across all of them.
- **Let a small sample read as "no effect".** Every inconclusive row
  reports the minimum effect that many observations could have detected.
- **Only return good news.** A significant move in the wrong direction is
  reported as `worsened`, at the same prominence as a win.

### Proving it, rather than observing it

`fleet_outcomes` above compares your fleet to its own past. That is useful
and it is *not causal* — a model upgrade or a shift in your task mix sits
in the same window. The randomized holdout removes that objection by
construction:

```bash
# an operator starts one for your org
python -m hub.manage start-experiment <org_id> 0.2    # withhold 20%
```

Your agents then ask before injecting, and report afterwards — over MCP
(`holdout_assign` / `record_occasion_outcome`), or from a shell:

```bash
commontrace prove assign ticket-8821 <trace-id> <trace-id> ...
#   INJECT   a1b2...        <- use these
#   WITHHOLD c3d4...        <- deliberately keep these back
commontrace prove record ticket-8821 --succeeded
```

```bash
commontrace prove outcomes        # what the experiment has established
```

Traces under `withhold` are deliberately kept back, so your fleet
generates its own control arm. The comparison is then two arms of the same
fleet in the same window, differing only by whether the memory was
injected — which is what makes it survive "what else changed that
quarter?".

What comes back is per-lesson: effect size, 95% CI, p-value with a
multiple-comparisons correction across lessons, and an explicit
`UNDERPOWERED` verdict so "cannot answer yet" never reads as "no effect".
`HURTS` is a first-class result — and it is the one correlational scoring
structurally cannot produce, because a lesson retrieved often *because* it
fires on hard tasks looks good by retrieval count and bad by outcome.

**The cost is real and bounded:** the withheld fraction gets a worse
product on purpose. That is the price of knowing whether the product works
at all. Nothing turns it on by default.

### Design the pilot before you run it

The expensive, silent failure is a pilot that reaches its last day and says
*not enough data yet*. The occasions are spent, the window is gone, and the
only fix had to be applied on day one.

```bash
commontrace experiment --plan --occasions 240 --detect 0.15   # size it
commontrace experiment --configure --rate 0.5                 # then set it
```
```
To detect an effect of 15% against a 78% baseline at 80% power:
- 119 observations in EACH arm.
- At a 10% holdout that is 1,190 occasions.

240 occasions can answer this, but not at 10%. Set the holdout rate to 50%.
```

The arithmetic nobody does in their head: **at a 10% holdout only one
occasion in ten lands in the control arm, so a run reaches an answer about
ten times slower than its occasion count suggests.** The plan reads your own
observed baseline, names the rate your budget needs, and says plainly when no
rate can answer it at all — which is the most useful thing it can tell you,
and it is worth knowing before the window rather than after.

It also states the cost rather than selling the upside alone: a wider holdout
means that share of the work runs without its memory while the experiment is
live.

`--configure` writes the rate to the store, and **every retriever reads it** —
`commontrace query --experiment` and the MCP `retrieve` tool alike, so the two
cannot drift apart. Changing the rate **starts a fresh randomization**: since
assignment is `hash(lesson, occasion, salt) < rate`, a new rate re-randomizes
every occasion, and pooling the assignments from before and after would let
one occasion sit in both arms. The salt rotates so that is explicit — the
report scopes to the current run and names the earlier one rather than mixing
them (`--salt <salt>` reads it).

**"No measurable effect" is reserved for a sample that could have detected
one.** Clearing the per-arm floor is a condition for running the test, not
evidence the test could see anything — at 10 observations per arm the minimum
detectable effect is 61 percentage points. A null from a design that could not
have seen a 10-point change is reported as `UNDERPOWERED`, because that is
what it is. A *significant* result at small n keeps its verdict: power governs
how to read a null, not a finding.

### Auditing the instrument

An effect size is worth what it survives, and the first question a
data-science function asks is not "what was the p-value" — it is **"how do
you know that number isn't an artifact of who got measured?"**

Every report now answers that before it shows a number.

The estimate is computed only on occasions that got an outcome recorded.
Dropping the rest is the right handling — an agent that crashed before
reporting is missing data, and scoring it as a failure would penalise the
arm that crashed more — but it is unbiased **only if both arms lose
outcomes at the same rate**. They have a specific reason not to: the
withheld arm is, by construction, the one working without its memory, so it
is the arm more likely to run long, escalate, or be abandoned before anyone
writes up how it went. The treatment effect leaks into who gets measured.

Here is what that costs, from this repo's own test suite — a fleet of 600
occasions where the lesson does **nothing**, both arms succeeding at exactly
50%, with the single asymmetry that a withheld occasion which failed often
never gets reported:

| | |
|---|---|
| True effect | **0.0%** |
| Reported without the audit | **HURTS, −12.6%** |
| 95% CI | **[−20.8%, −4.4%]** — does not contain zero |
| p | **0.003**, significant, adequately powered |

It does not error, return empty, or read as underpowered. It reads as a
clean finding pointing the wrong way about a lesson that was fine — and
because `HURTS` is a first-class result here, a customer would have retired
it.

Five checks now run before any effect is shown, on both tiers:

| Check | Catches |
|---|---|
| **Differential attrition** | One arm being less likely to get an outcome recorded — and it reports the *direction*, because which arm loses data decides which way the number is wrong |
| **Arm balance** | A realized holdout share far from the configured rate. Assignment is a deterministic hash, so this is not luck |
| **Mid-run re-randomization** | A changed salt or rate, which silently makes the log two experiments pooled into one comparison |
| **Conflicting arms** | One (lesson, occasion) counted as evidence for *and* against the same lesson |
| **Outcome variation** | An outcome nothing can fail, which yields a difference of exactly zero and reads as a confident null |

Alongside them, a **power projection**: how far each lesson is from being
answerable, and roughly when at the current rate. `UNDERPOWERED` on day 30
is a spent pilot; the same fact on day 3 is a holdout rate you can still
change. The control arm almost always binds, and the reason is arithmetic —
at a 10% holdout it takes ~100 occasions to put 10 in the control, so a run
answers about ten times slower than its occasion count suggests. The report
says so, and names the rate that fixes it.

**And the treatment has to hold still, too.** A lesson is a file, and
`lesson approve`, a text editor and an agent calling `draft_lesson` all
rewrite it in place. Edit one on day 10 of a 30-day run and the occasions
before and after were treated with different instructions — pooled into one
arm, reported as one effect, for a treatment that is an average of two.

So a lesson now has a **revision**: a short digest over exactly the fields an
agent receives. Every assignment records which revision it was made against,
every effect is reported as `lesson_x @ a3f9c1d2` rather than against a
mutable name, and a lesson that moved mid-run is flagged with both revisions
in the order they happened:

```bash
commontrace lesson history lesson_backoff
#   (new)        -> d5503396812b   by cli:alice,  initial
#   d5503396812b -> cab6c7f1707c   by mcp:agent,  widened after three more traces
```

The digest covers what an agent *reads* and nothing else — `uses` and
`last_hit` change on every single retrieval, so hashing them would flag every
experiment inside a week, which is the false positive that teaches people to
ignore a validity report. Every content change is journaled append-only with
who changed it and why, so *what instruction was this fleet following on
March 4th, who approved it, and what did withholding it do* is a question
with an answer — including the actual TEXT, not just which revision it was:

```bash
commontrace lesson history lesson_backoff --as-of 2026-03-04
# prints the applies_when/do_not_apply_when/body that were live on that date,
# reconstructed from the journal's before/after content on each entry
```

(Content recorded from the point this field was added onward — a change
journaled before that upgrade still shows only its hash, and `--as-of`
says so plainly rather than fabricating text it does not have.)

Three things it will not do:

- **It will not correct the estimate.** Nothing can recover an outcome that
  was never recorded, so a compromised run gets a refusal to report a
  number, not a repaired one. `--strict` fails it.
- **It cannot detect contamination.** An agent that uses a lesson it was
  told to withhold leaves no trace and biases the effect toward zero. That
  is honoured by the client or not at all — and every report says so, out
  loud, because silence would read as coverage.
- **It will not treat a clean result as proof.** These catch the failures
  that leave a trace in the assignment log. That set is not everything, and
  the report says which is which rather than only listing problems: a
  caller cannot distinguish "checked, clean" from "not checked" when only
  failures appear.

## Your team's console

Everything above is a CLI or an MCP tool. The person who approves the renewal
is neither of those, so the Hub serves a console at `/app` scoped to one
organisation by its own API key:

| Page | What it answers |
| --- | --- |
| **Overview** | How much memory this fleet has, how many agents it runs, and how often a search comes back with nothing |
| **Proof** | Is the memory working — with **"can this be trusted?" rendered above the effect sizes**, not in a footnote under them |
| **Memory** | The corpus, searched the way the agents search it, showing which terms matched and which were too common to discriminate |
| **Knowledge Base** | Proposals sent, consultations used, credit earned |

```bash
HUB_CONSOLE_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
# unset, and no /app route exists at all
```

Four properties, each ruling something out:

- **It writes no queries of its own.** Every number comes from a function
  that already takes and filters on `org_id`. A cross-tenant leak is the
  worst failure this product has available, so the isolation argument rests
  on the one set of filters the tenant-isolation suite already exercises.
- **It is read-only.** Everything you could change from a browser alters
  either a measurement or a shared corpus, and both already have audited,
  authenticated paths. That is also why there are no CSRF tokens: there is
  no state-changing request for a forged one to trigger.
- **Revoking a key ends the browser sessions it opened**, checked on every
  request. Revocation that leaves a session alive for another eight hours is
  a false belief about the state of a credential.
- **When validity is COMPROMISED the effect sizes are withheld, not
  caveated.** On a page read in a renewal conversation, a number on screen
  gets quoted and the note beneath it does not travel with it.

It is separate from the operator console at `/admin`, in audience, in auth,
and in blast radius — and it is gated on a different secret, because one
value that both authenticates the vendor and signs customer sessions means
one leak compromises both.

---

### What it was worth

An effect size is a percentage. A renewal conversation is about a quantity.

```bash
commontrace experiment --value-per-occasion 24
```
```
**+67 occasions** went differently because of this memory, over the measured
window (95% CI +37 to +98).

At the 24.00 per resolved occasion you supplied, that is **+1,616**.
```

For every memory whose causal effect the holdout has *established*, that is
`effect × times injected` — how many more occasions went well because it
existed, carrying the confidence interval through. On the Hub it is the
`value_delivered` tool and a section on your console's Proof page.

**The count is measured here; the rate is yours.** This repository attaches no
currency to anything. You say what one resolved occasion is worth to your
organisation; nothing about that is stored.

**And evidence expires.** An effect estimate is a statement about the world
*at the time it was measured*. Six months later the API the lesson described
is deprecated and the policy it encoded has changed — but the estimate is
unchanged, because nothing re-ran it. Past a 180-day horizon a memory stops
being billed, and `value_delivered` tells you which memories to re-run the
holdout for, oldest first.

The rule is deliberately **not symmetric**, and the asymmetry is the point. A
memory measured as *harmful* contributes a negative number and reduces the
figure. If staleness simply expired every old verdict, a stale harm would
stop counting and the invoice would go **up** — a vendor deleting its own
harms by waiting long enough. So:

| Stale verdict | What happens | Effect on the invoice |
|---|---|---|
| HELPS | stops counting — you cannot bill for value you can no longer show is current | down |
| HURTS | **keeps** counting until re-measured — a harm you stopped looking at is not a harm that went away | down |

Both move the figure down. That is the rule, not a coincidence: when evidence
decays, it resolves against the party who benefits from the doubt. Evidence
carrying no date at all is treated exactly as expired.

Three rules, and the third is the one that makes the number worth quoting:

- **A compromised experiment produces no figure at all** — not a hedged one.
  If a named mechanism is biasing the effects it biases every value computed
  from them, and a value report is precisely where a caveat gets separated
  from the number it qualifies.
- **An underpowered memory contributes nothing.** Measured on a real run: one
  reporting +30% on 90 occasions, never established, would have added a
  phantom +27. That is how a null becomes a sales figure.
- **Memories measured as HURTING are subtracted, not dropped.** A figure that
  sums only the winners is a brochure. The whole claim here is that this will
  tell you when its own memory is making things worse — a number that quietly
  excludes those retracts the claim in the one document where it is being
  cashed.

### When an answer stops being right

Seeding and submissions both answer "how does content get in". Neither
answers what happens when the world moves and an entry stops being true —
and a curated corpus that only grows is one that decays. Every Knowledge
Base entry therefore carries a **standing**, computed from signals the
system was already collecting:

| Standing | Meaning |
|---|---|
| `disputed` | At least three fleets voted and a majority reported it did not work |
| `stale` | The entry declared a review date when it was written, and it has passed |
| `established` | Corroborated by enough fleets to be more than the author's confidence |
| `unproven` | In the corpus, not yet judged — where every entry starts |

You give that signal with `vote_trace` (`down` plus a `feedback_tag` of
`outdated`, `wrong`, or `security_concern`). What it changes:
`commons_overlap` stops counting a disputed entry as coverage — a wrong
answer is not a solved failure — and reports it separately under
`disputed_matches` instead, so a coverage number never moves without you
being able to see why. `commons ask` still shows the entry, ranked last
and labelled, because a contested answer plus the warning beats no answer.

**Votes inform; the operator decides.** No vote count withdraws anything.
The strongest automatic effect is a smaller coverage claim and a worse
rank — both of which make this product's own numbers more conservative,
never less. Withdrawing an entry is a human action
(`python -m hub.manage kb-retract`, reversible with `kb-restore`).

For an operator, that feedback is what makes curating a corpus scale past
what anyone could re-read: `kb-review` lists the entries that need a
decision — security flags first, then disputed, then past their review
date, then never-matched — each ordered by how much traffic it affects. So
review cost tracks the *error rate*, not the corpus size.

### Plans, and what they actually enforce

A plan is only real if the server refuses the request that exceeds it, and
that is implemented rather than described — `hub/plans.py`, enforced in
`hub/crud.py`.

Two things are metered, chosen so neither can charge for something the
customer did not get:

| | `free` | `team` | `scale` |
|---|---|---|---|
| Traces stored | 1,000 | 50,000 | unlimited |
| Knowledge Base queries / month | 20 | 1,000 | 25,000 |
| Active agents | 5 | 25 | unlimited |

Storage is real cost and grows monotonically. **Knowledge Base queries are
the metered unit** because that is the only call whose value comes from
content the org did not itself produce — everything else an org does is
with its own data, and charging per query against your own memory is rent,
not price. Purging frees storage allowance, so the deletion right in
`DATA_RETENTION.md` is not a right in name only. The flat plan grant above
is the floor, not the ceiling — see "Propose an entry" for the one way an
org can permanently raise it, by having a Knowledge Base submission
accepted rather than by the act of submitting.

**Active agents** is the expansion axis, and it is metered on `Trace.agent_id`
— *which* agent produced a trace, as opposed to `agent_type`, which is what
*kind* it is. A fleet of 25 support agents shares one `agent_type`, so only
`agent_id` can answer how many agents a fleet actually runs (see
[`commontrace capture --agent-id`](#4--capture-experience-and-curate-lessons)).
Three properties of the count are deliberate:

- **It counts agents active in a trailing 30-day window, not all-time.** An
  all-time count can only rise: it could never show a fleet shrinking, and it
  would bill for an agent that ran once and was decommissioned. Retiring an
  agent frees its slot, for the same reason purging frees storage.
- **The cap blocks expansion, never operation.** An org at its limit keeps
  serving every agent it already runs; only registering a *new* agent is
  refused. A commercial limit must not become a production outage, so
  lowering an org's plan below its current fleet size never breaks that
  fleet either — the overage surfaces in `manage usage` for a human.
- **It is a floor, not a total, when `agent_id` is missing.** Traces from a
  client that sends no `agent_id` are never rejected (that would break every
  client written before this existed) but they all collapse into one
  `unattributed` agent. `manage usage` marks those orgs with a trailing `+`
  rather than quoting the number as exact.

The earning mechanic that exists is narrow and deliberate: an *accepted*
Knowledge Base submission adds a permanent, one-time bonus
(`Organization.bonus_commons_queries`) on top of the plan's flat grant.
Nothing else moves this number — not submitting, not how much you submit,
not how many queries you run.

```bash
commontrace commons usage                      # what you have, what you've used
python -m hub.manage set-plan <org_id> team    # operator: move an org
python -m hub.manage usage                     # operator: every org's meter
python -m hub.manage revenue                   # billable orgs: consumption of both metered resources
```

Exceeding a limit returns `entitlement_exceeded` — deliberately *not*
`rate_limited`, because a rate limit clears by waiting and this does not; a
client that cannot tell them apart retries forever. The error carries
`metric`, `limit`, `used`, `plan` and `remedy` so a client can render an
upgrade path instead of a failure.

The meter is a shared table with an atomic `INSERT ... ON CONFLICT DO
UPDATE`, not a per-process counter. A read-modify-write meter loses
increments under concurrency, which hands out a discount in exact
proportion to how parallel — and therefore how large — the customer is;
`hub/tests/test_plans.py::TestMeteringIsAtomic` fails if that regresses.

**No currency appears anywhere in this repository, and that is deliberate.**
This implements the entitlement, not the invoice. Payment, tax, dunning,
refunds and disputes belong to a billing system, and printing a dollar
figure computed from a hardcoded rate would read as revenue reporting while
being arithmetic on a number nobody agreed to. `manage revenue` prints the
denominator a price should be argued from — per paying org, real
consumption of storage and Knowledge Base queries. There is no "delivered"
side to net against: customers do not contribute to what they consume in
this model, so consumption is the whole number, not one side of a ledger.

### The cold start

An empty Knowledge Base returns 0% to every prospect — by construction, not
as a finding — so a starter corpus of public substrate knowledge ships in
the repository to break that:

```bash
python -m hub.manage commons-seed commons/seed/substrate-v1.jsonl <operator_org_id>
```

`commons/seed/substrate-v1.jsonl` is 46 recurring substrate failures — the
at-least-once webhook, the exhausted connection pool, the `ADD COLUMN NOT
NULL` that locks the table, the JWT that fails on clock drift. Every record
carries a `source` citing where the knowledge comes from (public protocol
semantics, vendor documentation, standards), stored as the trace's
`shared_rationale` so a customer can always see the provenance of anything
it matched. Nothing in it is drawn from any fleet's private data, and
nothing in it is invented experience. `hub/tests/test_commons.py`
(`TestShippedSeedCorpus`) asserts the shipped file loads with zero skipped
lines and that every record is cited.

What it is *not* is a coverage claim. What fraction of a real fleet's
failures this corpus covers is measured separately, on held-out data, in
`commons/eval/` — see [Measuring coverage honestly](#measuring-coverage-honestly).

Seeded rows are marked `commons_source='seed'` — the only value any
Knowledge Base query will ever match against, so this is also the security
boundary, not just a label. `kb-stats` reports which entries are actually
answering real queries and which have never matched anything, so the
operator can tell curated substrate knowledge apart from filler that reads
well but never helps.

### Measuring coverage honestly

`commons_overlap` returns a percentage, and that percentage is what a
prospect decides on. So it is measured rather than asserted, against probes
the corpus was not built from:

```bash
python commons/eval/run.py        # commons/eval/RESULTS.md records the run
```

46 held-out positives (real failures the corpus contains, written
symptom-first in on-call vocabulary rather than the corpus's own wording)
and 22 negative controls (real substrate failures deliberately absent,
several chosen as near misses). At the shipped 0.30 threshold:

| Metric | Measured |
|---|---|
| Recall on positives | **10.9%** (5/46) |
| Of those matches, the right record | **100%** (5/5) |
| False positives on the 22 controls | **0%** |

Read plainly, and it is not the flattering result:

**The number a fleet sees is a floor, not an estimate.** When the Knowledge
Base genuinely contains a fleet's failure and the fleet describes it in its
own words, the matcher finds it about one time in nine. A prospect who sees
5% should conclude the Knowledge Base covers *at least* 5% of their
problems, not about 5%.

**What it does match, it matches correctly.** Every match landed on the
exact record it was written against, and not one absent failure was
reported as covered. The failure mode is silence, not noise — which is the
right direction for a number that gets quoted, and the reason it is safe to
quote at all.

**No threshold fixes it, so the threshold was not moved.** Recall only
becomes useful near 0.10, where 23% of *absent* failures get reported as
present. This is the representation, not the tuning: Jaccard similarity
over content words is lexical, and two engineers describing the same
substrate failure share almost no words. The honest fix is semantic
similarity, and it is not a tweak — embeddings require a model to see the
failure text, which is exactly what the current design refuses to transmit.
That trade is unresolved and is written down as unresolved.

`commons/eval/RESULTS.md` carries the sensitivity table and the limits in
full — the most important being that the same author wrote both the corpus
and the probes, which makes 10.9% an optimistic bound rather than an
estimate of a real fleet. The measurement that would settle it is a run
against failures a customer actually collected, and it has not happened.

---

## Deploying to Production

CommonTrace is built to run as production infrastructure, not as a trial.
The two tiers deploy independently: the **Local tier** is flat files in a
git-tracked `memory/` directory with no service to operate, and the
**Hub tier** is a containerized MCP server backed by PostgreSQL. Either
can be adopted without the other.

### What it connects to

Agent sessions and traces, feedback and outcomes,
existing memory, observability data, and failure/escalation logs. Concretely:
`commontrace capture` for sessions and outcomes; `commontrace init` to adopt
an existing memory directory as the Local tier (§ [Memory System](#memory-system));
`commontrace sync` to pull/push against the CommonTrace Hub if the fleet
already has traces there.

### What it does

Finds repeated failure patterns (`repeated_error`
outcome tagging + `memory/lessons/` domain coverage), extracts candidate
lessons (Curator role, § [Roles](protocol/PROTOCOL.md#6-roles-generalized)),
validates and approves them before deployment (Validator role — an explicit
human approval gate is exactly what `status: review → active` models),
injects them into relevant decisions (Retriever role), and continuously
measures performance against a baseline (`commontrace bench --pilot`).

Nothing reaches a live agent without passing a human gate: a lesson stays at
`status: review` until someone promotes it. Rollback is `commontrace lesson
reject` (or a git revert of `memory/`) and takes effect on the next
retrieval — there is no cache to drain or model to retrain.

### Rolling it out

1. `commontrace init --agent-type <type>` on the target fleet's workflow.
2. Backfill historical outcomes as baseline traces
   (`commontrace capture ... --baseline`, or `commontrace import` for a bulk
   JSONL/CSV export). This is what "before" is measured against, so it is
   worth doing properly — but it needs no infrastructure change, since the
   Local tier is flat files.
3. Turn on live capture (drop `--baseline`) and start curating lessons
   (`commontrace lesson new`, gated at `status: review` until approved).
4. Wire retrieval into the fleet (`commontrace install --target <platform>`)
   so lessons get reinjected before each decision.
5. Deploy the Hub if more than one workflow or team needs to share traces —
   see [`hub/DEPLOYMENT.md`](hub/DEPLOYMENT.md) for the container, the
   Postgres schema migration, health probes, and the post-deploy smoke check
   (`python -m hub.smoke`).

### Running it in production

The Hub is designed for continuous operation under real multi-tenant load,
and the properties that matter most are enforced in code and covered by
tests, not left to convention:

| Concern | How it's handled |
|---|---|
| Tenant isolation | Every trace read/write is scoped by `org_id` in the SQL `WHERE` clause, never filtered in Python. Covered by `hub/tests/test_tenant_isolation.py`. |
| Authentication | Per-org API keys, argon2id-hashed, never stored or logged in recoverable form. Rotate with `manage.py rotate-key`, revoke with `revoke-key`, and set an expiry at issue time. |
| Retries | `contribute_trace` accepts an `idempotency_key`, so a client retrying after a lost response gets the original result instead of a duplicate trace. |
| Concurrency | Vote writes are a single atomic upsert; counters use in-database increments. Concurrent-load regression tests run against real Postgres in CI. |
| Health checks | `/healthz` (liveness, no DB dependency — a database blip must not trigger a restart storm) and `/readyz` (readiness, real query). |
| Observability | Structured JSON logs with a per-request correlation id; CI fails the build if an API key or DB password ever appears in log output. |
| Audit trail | Every mutation and every operator action writes a content-free audit row that survives the data it describes. |
| Data deletion | Self-service via an org's own API key (`delete_trace`; `request_account_deletion`/`confirm_account_deletion` for a whole org, two calls with a mandatory delay between them), or operator-CLI (`manage.py purge-trace`/`purge-org`). All four perform hard deletes and follow amendment chains. See [`DATA_RETENTION.md`](DATA_RETENTION.md). |
| Data retention | Per-org policies by object type and status, a purge plan you read before anything happens, and legal holds that outrank every policy. `manage.py set-retention` / `retention-plan` / `retention-apply` / `legal-hold`. See [`DATA_RETENTION.md`](DATA_RETENTION.md) §2. |
| Event export | Signed, at-least-once webhooks carrying ids, counts and verdicts — **never trace content**, enforced by a per-event-type field whitelist. `manage.py webhook-add`. See `hub/README.md` "Event export". |
| Rate limiting | Per-org token bucket. **Known limitation:** it is process-local, so N replicas allow roughly N× the configured rate — see `hub/DEPLOYMENT.md` §6 for the mitigations. |

**What this does *not* have** is as important as the table above, and is
written down rather than left to be discovered: no legal entity, no SOC 2,
no penetration test, no SAML or browser-based login (OIDC, SCIM and human
user accounts do exist), no residency commitment and no support SLA. [`TRUST.md`](TRUST.md) states the trust boundary and lists every gap at
full weight; [`AUDIT_RESPONSE.md`](AUDIT_RESPONSE.md) answers a third-party
readiness audit finding by finding, marking each one done, partial, not
applicable, or *requires business action* — with the rule that the last
category is never quietly downgraded by building something adjacent to it.

Before putting a client's data on it, work through the security checklist in
`hub/DEPLOYMENT.md` §10 and the deliberately-documented limitations in §11.
Backups are the one piece CommonTrace does not implement for you: the Hub
keeps all state in Postgres, so your provider's automated backups (or
`pg_dump`) cover it — but set a retention window and **test a restore**,
because an untested backup is not a backup.

### Measuring, continuously

`commontrace bench --pilot` gives before/after deltas on the five outcome
metrics above. Run it on a schedule, not once: the first read after ~30 days
of live capture tells you whether adoption helped, and the same command a
year later tells you whether it still does. Pair it with the protocol-health
axes in [Benchmark](#benchmark) as a secondary signal, and with
`commontrace experiment` when you need a causal answer for a specific lesson
rather than a correlational one.

---

## File Layout

```
commontrace-v2/
  protocol/
    PROTOCOL.md               — Canonical, implementation-independent protocol spec
    schemas/
      trace.schema.json        — Universal Trace object (matches the Hub server's on-wire shape)
      lesson.schema.json       — Local governance wrapper (importance, applies_when, status)
  commontrace/                 — The `commontrace` CLI (pip-installable client)
    cli.py, paths.py, frontmatter.py, trace_io.py, validate.py, templates.py, hub_client.py,
    distill.py (Curator clustering), retrieval.py (lexical fallback ranker), import_data.py,
    reliability.py + evidence_io.py (lesson scoring/contradictions), experiment.py (causal
    holdout), taxonomy.py + impact.py + pilot.py (the 30-day pilot's three leave-behinds),
    report_html.py (shared HTML shell for taxonomy/impact/pilot --html)
    commands/                  — init, install, capture, import, trace, distill, lesson, query,
                                  index, bench, reliability, experiment, taxonomy, impact, pilot,
                                  sync, doctor
    schemas/                   — bundled copy of protocol/schemas/*.json (works without a repo checkout)
  hub/                          — The Hub server (self-hosted; see hub/README.md to run one)
  pyproject.toml               — packaging config for the `commontrace` CLI. Currently
                                  installed via `pip install -e .` from a checkout; not
                                  yet published to PyPI (`pip install commontrace` is the
                                  intended path once it is).
  SKILL.md                     — Code-review reference profile spec (pipeline, agent briefs)
  DOCUMENTATION.md             — Deep-dive on the code-review profile: design decisions, research refs
  README.md                    — This file
  AGENTS.md                    — Agent-facing guidance (any platform)
  requirements.txt             — Python deps for the code-review profile's attention layer
  install.sh                   — Setup script for the code-review profile (SKILL.md route)
  assets/                      — Architecture diagrams (.dot + .png)
  memory/                      — Local store: lessons/ (any agent_type), traces/ (generic), episodes/ (code profile)
  benchmark/                   — measure_performance.py (protocol health) + pilot_metrics.py (business outcomes)
  tests/                       — pytest suite (frontmatter contract, benchmark, CLI)
  .devin/                      — Devin-specific skill config (optional)
```

---

## Requirements

### CLI (any agent type)
- **Python** 3.10+
- `pip install -e .` — one dependency (PyYAML)
- Optional: `pip install -e ".[attention]"` for semantic retrieval (`numpy`, `sentence-transformers`, pulls torch)

### Code-review reference profile (`SKILL.md`)
- **Python** 3.10+
- **pip packages** (see `requirements.txt`):
  - `numpy>=1.24`
  - `sentence-transformers>=2.7` (installs torch automatically)
  - `PyYAML>=6.0`
- **Any AI agent** that can read `SKILL.md` and spawn sub-agents
- **git** (used for commit-after-A in Phase 4)
- Disk: ~500 MB for the HuggingFace model cache (one-time download)
