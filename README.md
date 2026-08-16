# commontrace

> **Version:** v2.3 | **Status:** trial-ready | **Python:** 3.10+

A provider-agnostic double-review agent pipeline for AI coding assistants.
An **Implementer (A)** and an independent **Reviewer (B)** iterate until the task passes,
with automatic long-term memory so lessons learned in one run are applied in future runs.

Works with **any agent platform** — Devin, Claude Code, Cursor, OpenHands, or custom
orchestrators that can spawn sub-agents and read a spec file.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [How It Works](#how-it-works)
3. [Architecture](#architecture)
4. [Configuration](#configuration)
5. [Memory System](#memory-system)
6. [Benchmark](#benchmark)
7. [File Layout](#file-layout)
8. [Requirements](#requirements)

---

## Quick Start

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
# Markdown summary to stdout (scripts auto-detect their root)
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

---

## File Layout

```
commontrace/
  SKILL.md                   — Full skill specification (pipeline, agent briefs)
  DOCUMENTATION.md           — Deep-dive: design decisions, research refs, roadmap
  README.md                  — This file
  AGENTS.md                  — Agent-facing guidance (any platform)
  requirements.txt           — Python dependencies
  install.sh                 — Setup script
  assets/                    — Architecture diagrams (.dot + .png)
  memory/                    — Long-term memory store
  benchmark/                 — Memory health benchmark
  .devin/                    — Devin-specific skill config (optional)
```

---

## Requirements

- **Python** 3.10+
- **pip packages** (see `requirements.txt`):
  - `numpy>=1.24`
  - `sentence-transformers>=2.7` (installs torch automatically)
  - `PyYAML>=6.0`
- **Any AI agent** that can read `SKILL.md` and spawn sub-agents
- **git** (used for commit-after-A in Phase 4)
- Disk: ~500 MB for the HuggingFace model cache (one-time download)
