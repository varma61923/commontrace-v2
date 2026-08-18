# commontrace

> **Protocol version:** 1.1.0 | **Code-review reference profile:** v2.3 | **Status:** trial-ready | **Python:** 3.10+

CommonTrace is an **agent-agnostic protocol** for turning agent experience into
validated, reusable lessons: Capture → Structure → Extract → Validate → Store →
Inject → Measure. The full spec lives in [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md).

This repo ships two things:

1. **The `commontrace` CLI** (`pip install -e .`) — client-installable, works with
   any agent fleet (code, support, sales, HR, marketing, ...), and can wire a local
   store into Claude Code, Cursor, Devin, Windsurf, or any generic MCP client. It
   also bridges to the production **CommonTrace Hub** (a live, cross-org shared trace
   store reachable over MCP — `search_traces`, `contribute_trace`, `get_trace`,
   `vote_trace`, `amend_trace`, `list_tags`).
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

### 3 — Wire it into your agent platform

```bash
commontrace install --target claude-code      # writes .claude/skills/commontrace/SKILL.md
commontrace install --target cursor           # writes .cursor/rules/ + a Hub MCP config template
commontrace install --target devin            # writes .devin/skills/commontrace/SKILL.md
commontrace install --target windsurf         # writes .windsurf/rules/
commontrace install --target generic-mcp      # Hub MCP config template for any other MCP client
```

### 4 — Capture experience and curate lessons

```bash
commontrace capture --title "..." --context "..." --solution "..." --tags a,b --agent-type support
commontrace lesson new --slug lesson_x --description "..." --domain escalation \
  --agent-type support --applies-when "..." --do-not-apply-when "..." --importance 4 \
  --importance-rationale "..."
commontrace lesson validate      # checks against protocol/schemas/lesson.schema.json
commontrace trace validate       # checks against protocol/schemas/trace.schema.json
commontrace sync                 # how to bridge this store to the CommonTrace Hub
```

Add `--resolved` / `--escalated` / `--repeated-error` / `--frustration` /
`--tokens-used N` / `--llm-calls N` / `--baseline` to `capture` to record the
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
      trace.schema.json        — Universal Trace object (matches the live Hub API)
      lesson.schema.json       — Local governance wrapper (importance, applies_when, status)
  commontrace/                 — The `commontrace` CLI (pip-installable client)
    cli.py, paths.py, frontmatter.py, trace_io.py, validate.py, templates.py
    commands/                  — init, install, capture, trace, lesson, query, index, bench, sync, doctor
    schemas/                   — bundled copy of protocol/schemas/*.json (works without a repo checkout)
  pyproject.toml               — `pip install commontrace` packaging
  clients/                     — (see `commontrace install --target ...`) generated platform integrations
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
