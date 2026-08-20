# commontrace

> **Version:** 2.0.0 | **Code-review reference profile:** v2.3 (versioned independently, see [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md#9-versioning--extension-mechanism)) | **Status:** trial-ready | **Python:** 3.10+

CommonTrace is an **agent-agnostic protocol** for turning agent experience into
validated, reusable lessons: Capture → Structure → Extract → Validate → Store →
Inject → Measure. The full spec lives in [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md).

This repo ships two things:

1. **The `commontrace` CLI** (`pip install -e .`) — client-installable, works with
   any agent fleet (code, support, sales, HR, marketing, ...), and can wire a local
   store into Claude Code, Cursor, Devin, Windsurf, or any generic MCP client. It
   also bridges to the **CommonTrace Hub** (a self-hostable, cross-org shared trace
   store reachable over MCP — `search_traces`, `contribute_trace`, `get_trace`,
   `vote_trace`, `amend_trace`, `list_tags`; server implementation and setup in
   [`hub/`](hub/README.md), not a hosted service run by this project).
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
2. [Quick Start — Code Agent reference profile](#quick-start--code-agent-reference-profile)
3. [How the Reference Pipeline Works](#how-it-works)
4. [Architecture](#architecture)
5. [Configuration](#configuration)
6. [Memory System](#memory-system)
7. [Benchmark](#benchmark)
8. [Pilot Metrics](#pilot-metrics)
9. [The 30-Day Fleet-Learning Pilot](#the-30-day-fleet-learning-pilot)
10. [File Layout](#file-layout)
11. [Requirements](#requirements)

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
commontrace init --agent-type support        # or: sales | hr | marketing | code | ops | custom
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

Field names are configurable (`--title-field`/`--context-field`/
`--solution-field`/`--tags-field`/`--id-field`) since a real export's column
names are whatever the source system calls them. Rows missing a required
field are skipped and reported, not silently dropped or a hard failure of
the whole batch. `resolved`/`escalated`/`repeated_error`/
`frustration_signal`/`tokens_used`/`llm_calls` columns, if present, populate
`Trace.outcome` (§ [Pilot Metrics](#pilot-metrics)) automatically.

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

### 4 — Capture experience and curate lessons

```bash
commontrace capture --title "..." --context "..." --solution "..." --tags a,b --agent-type support
commontrace lesson new --slug lesson_x --description "..." --domain escalation \
  --agent-type support --applies-when "..." --do-not-apply-when "..." --importance 4 \
  --importance-rationale "..."
commontrace lesson validate      # checks against protocol/schemas/lesson.schema.json
commontrace trace validate       # checks against protocol/schemas/trace.schema.json
commontrace sync                 # push active lessons + pull search results, if a Hub is configured
commontrace sync --push          # push only
commontrace sync --pull --query "..." --tags a,b   # pull only
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
```

`distill` clusters traces by word-overlap similarity (pure Python, no LLM
call, no API key) and never writes anything above `status: review` — a
candidate only becomes retrieval-eligible once a human runs `lesson
approve`. Traces already referenced by an existing lesson's `source_traces`
are skipped on the next run, so re-running `distill` doesn't keep
re-proposing patterns someone already curated.

### 6 — Measure what another fleet's lessons would be worth to you

```bash
commontrace overlap sign --fleet-label acme --out acme.json      # signatures, not content
commontrace overlap report --ours acme.json --theirs partner.json
```

Answers *"of the failures we keep hitting, how many has another fleet
already solved?"* — the quantity the cross-org value proposition depends on
and which has never been measured (see [`STRATEGY.md`](STRATEGY.md)).
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

Nothing is changed automatically; the Validator gate stays human.

### 8 — Retrieve a lesson for an incoming task

```bash
commontrace query "customer is escalating about a delayed refund"
commontrace query "..." --lexical        # force the dependency-free fallback
```

Falls back automatically to a pure-Python lexical (word-overlap) ranker if
the optional `attention` extra (semantic embeddings) isn't installed —
`commontrace query` always returns something with just the core install,
rather than failing outright.

### 9 — Prove the lessons *cause* the improvement

```bash
commontrace query "..." --experiment --occasion-id task-4711   # withhold at random, log the arm
# ...do the task, then record its outcome under that same id...
commontrace experiment                  # causal effect per lesson
commontrace experiment --strict         # non-zero exit if a lesson significantly HURTS
```

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
- "No measurable effect" is always reported alongside the **minimum
  detectable effect** for that sample, so it reads as a statement about the
  experiment's power rather than about the lesson.

The cost is bounded and explicit: in the worst case the lesson would have
helped, and 1 occasion in 10 loses that help. That is the price of knowing.

Add `--resolved`/`--not-resolved`, `--escalated`/`--not-escalated`,
`--repeated-error`/`--not-repeated-error`, `--frustration`/`--not-frustration`,
`--tokens-used N`, `--llm-calls N`, and `--baseline` to `capture` to record the
outcome data behind the pilot metrics (§ [Pilot Metrics](#pilot-metrics)
below) — all optional, all additive to the base capture.

See [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md) for the object model these
commands produce, and `commontrace --help` / `commontrace <subcommand> --help`
for the full CLI reference.

---

## Quick Start — Code Agent reference profile

### 1 — Install

```bash
git clone https://github.com/denemlabs/commontrace-v2 commontrace
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
python3 benchmark/measure_performance.py

# Last 5 episodes only
python3 benchmark/measure_performance.py --n=5

# HTML report
python3 benchmark/measure_performance.py --html
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

Plus an **attention layer** (`memory/attention/`) — a local numpy/sentence-transformers
embedding index that pre-filters lessons for Alpha at scale (100+ lessons without latency
degradation).

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
python3 memory/attention/query.py "my task"
python3 benchmark/measure_performance.py
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
    build_index.py      — builds embedding index from active lessons
    query.py            — pre-filters lessons by cosine similarity (used by Alpha)
    index.npz           — generated file (gitignored, rebuild with build_index.py)
```

**Memory starts empty.** The example entries in `memory/lessons/` and `memory/episodes/`
are illustrative templates — delete them once your own runs accumulate.

**Rebuild the attention index** after adding or editing lessons:

```bash
python3 memory/attention/build_index.py
# or force-rebuild:
python3 memory/attention/build_index.py --force
```

The embedding model (`multi-qa-mpnet-base-dot-v1`, ~420 MB) is downloaded once and
cached under `~/.cache/huggingface/`.

---

## Benchmark

`benchmark/measure_performance.py` measures memory health across three axes:

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

## Pilot Metrics

`benchmark/pilot_metrics.py` (via `commontrace bench --pilot`) computes the
five business-outcome metrics from the pilot deck, from `Trace.outcome` data
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

Traces marked `--baseline` are compared against everything else, giving the
same before/after framing the deck reports for the Loops pilot ("-53% time to
resolve," "-29% churn") — computed from your own fleet's traces, not ours.
Any `outcome` field left unset is simply excluded from its metric rather than
counted against you, so you can adopt outcome tracking incrementally.

---

## The 30-Day Fleet-Learning Pilot

The intended shape of a first deployment, matching the pilot deck:

**What we connect to** — agent sessions/traces, feedback and outcomes,
existing memory, observability data, failure/escalation logs. Concretely:
`commontrace capture` for sessions and outcomes; `commontrace init` to adopt
an existing memory directory as the Local tier (§ [Memory System](#memory-system));
`commontrace sync` to pull/push against the CommonTrace Hub if the fleet
already has traces there.

**What CommonTrace does** — finds repeated failure patterns (`repeated_error`
outcome tagging + `memory/lessons/` domain coverage), extracts candidate
lessons (Curator role, § [Roles](protocol/PROTOCOL.md#6-roles-generalized)),
validates and approves them before deployment (Validator role — an explicit
human approval gate is exactly what `status: review → active` models),
injects them into relevant decisions (Retriever role), and compares
performance against a baseline (`commontrace bench --pilot`).

**What we measure** — the five metrics above, plus the protocol-health axes
in [Benchmark](#benchmark) as a secondary signal.

**Low-risk setup** — no infrastructure replacement (Local tier is flat
files); start from historical traces (`commontrace capture --baseline` on
existing logs, backdated); approve lessons before deployment (`status:
review`, promoted to `active` only after a human/Validator pass); compare
against an existing baseline (built into `bench --pilot`).

**Runbook**:
1. `commontrace init --agent-type <type>` on the target fleet's workflow.
2. Backfill 1-4 weeks of historical outcomes as baseline traces
   (`commontrace capture ... --baseline`).
3. Turn on live capture (drop `--baseline`) and start curating lessons
   (`commontrace lesson new`, gated at `status: review` until approved).
4. Wire retrieval into the fleet (`commontrace install --target <platform>`)
   so lessons get reinjected before each decision.
5. After ~30 days, `commontrace bench --pilot` for the before/after deltas.

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
    distill.py (Curator clustering), retrieval.py (lexical fallback ranker), import_data.py
    commands/                  — init, install, capture, import, trace, distill, lesson, query, index, bench, sync, doctor
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
