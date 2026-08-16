# `memory/lessons/` — Lessons capitalized across /commontrace runs

## Role

A lesson `lesson_<slug>.md` represents a rule learned from at least one past episode, formulated in natural language so it can be:
1. **Retrieved** by the sub-agent **Alpha** (Phase 0) based on `name` / `description` / `tags` / `domain` / `applies_when`
2. **Applied** in the A brief of the new run (recommendations, anti-patterns, docs to read)

## Writing Workflow

Lessons are NEVER written directly by Omega. Pipeline (v2.2):

1. **Phase 10 (Omega)** proposes 0-N candidate lessons (verbatim output).
2. **Phase 11 (Lambda)** audits proposals against 4 criteria (formal quality, non-duplicate, generalization, importance calibration) and renders a verdict ACCEPTED | REJECTED | NEEDS REFINEMENT per proposal.
3. If Lambda marks ACCEPTED -> the **orchestrator** creates the file `lesson_<slug>.md` HERE with strict YAML frontmatter, then updates `memory/INDEX.md` (corresponding domain section).

Workflow is 100% automated: no user validation in the loop, usable by an agent without a human.

## Update Workflow

When an existing lesson is hit (useful in a run):

1. Omega proposes in Phase 10: `UPDATE LESSON "lesson_xxx": +1 uses`.
2. Lambda audits in Phase 11 (consistency: source_episode not already present, dates consistent, uses consistent, lesson_hit confirmed).
3. If ACCEPTED -> orchestrator increments `uses` in the frontmatter, sets `last_hit: YYYY-MM-DD`, appends the current episode to `source_episodes`.

## Revision Workflow

If a lesson was retrieved by Alpha but turned out to be non-applicable (poor formulation, applies_when too broad):

1. Omega flags in Phase 10: `REVISION LESSON "lesson_xxx": NEEDS REVISION — reason`.
2. Lambda audits in Phase 11 (reason documented concretely with citation from A/B report or orchestrator retro).
3. If ACCEPTED -> orchestrator changes `status: active -> review` in the frontmatter and appends a `## Revision note` comment in the body.

The user can then manually edit lessons with `review` status.

## Format

Strict YAML frontmatter (parsable by `yaml.safe_load`), followed by a markdown body with fixed sections. See `lesson_template.md` for an empty skeleton.

### Required Frontmatter Fields

| Field | Type | Description |
|---|---|---|
| `name` | string | unique slug of the lesson (e.g. `lesson_subagent_double_review_pattern`) |
| `description` | string | 1-line summary — used by Alpha for semantic filtering |
| `tags` | list[string] | Free tags (e.g. `[subagents, pattern, double-review]`) |
| `domain` | enum | `git-safety` \| `cuda-gpu` \| `refactor` \| `testing` \| `subagents` \| `performance` \| `other` |
| `importance` | int | Integer 1-5 — see full rubric in `SKILL.md` section "Importance rubric" (5=showstopper, 4=critical, 3=useful, 2=minor, 1=anecdotal). Single source of truth; `INDEX.md` reflects this value. |
| `importance_rationale` | string | 1-sentence concrete REQUIRED justification for the score (not generic). Good example: "Without this rule, silent overwrite by parallel sub-agents"; bad: "important because useful". |
| `importance_history` | list[dict] | Log of importance changes, format `[{date: YYYY-MM-DD, old: N, new: M, reason: "..."}]`. Initialized `[]`. Useful for audit + calibration drift detection. |
| `applies_when` | string | Precise semantic activation condition (>= 1 concrete sentence) — Alpha uses this to decide whether to apply |
| `do_not_apply_when` | string | Explicit counter-condition — prevents over-generalization |
| `uses` | int | Usage counter (incremented in Phase 11) |
| `last_hit` | string | `YYYY-MM-DD` of the last hit, or `NEVER` |
| `source_episodes` | list[string] | Slugs of episodes that contributed to this lesson |
| `status` | enum | `active` \| `review` (flagged for revision) \| `archived` (manually) |

### Omega Criteria for Proposing a New Lesson (v2.1)

Omega proposes a candidate lesson if at least ONE of the following two criteria is true (in addition to "not covered by an existing lesson" AND "generalizable cross-project"):

- **(A)** Source episode importance >= 3 AND generalizable cross-project.
- **(B)** Importance 4-5 even on a single occurrence — a showstopper / critical issue deserves to be captured immediately.

Replaces the old criterion ">= 2 episodes showing it" which was too strict for rare but critical showstoppers.

The candidate lesson's importance is derived from the `source_episodes` (max or mean), adjustable +/- 1 by Omega at proposal time (justify in the proposal).

### Body Sections

- **## Rule** — 1 actionable sentence
- **## Why** — factual observation or source incident, grounded in project reality
- **## How to apply** — when to invoke it, how to use it concretely in the A or B brief
- **## Counter-examples** — cases where the rule does NOT apply

## Best Practices for Writing a Good Lesson

- **applies_when** must be precise: not "when refactoring" but "when doing an architectural refactor touching >= 3 files of a module"
- **do_not_apply_when** must explicitly list known exceptions: not "except in special cases" but "does not apply to throwaway R&D scripts" or "does not apply if tested with TDD"
- **Why** must cite a concrete incident or observation from a project, not a generality
- **How to apply** must be actionable: "add line X in the A brief" or "verify Y before running the code", not "be careful"

## Domain Hierarchy

Lessons are organized semantically in `INDEX.md` by `domain`. Current domains:

- `git-safety` — git operations, commit, recovery, stash/clean/reset
- `cuda-gpu` — CUDA kernels, full-GPU, host syncs, atomics, determinism
- `refactor` — architectural refactor, strict rename, copy vs reimplement
- `testing` — existing tests, pytest, empirical parity, skip/xfail
- `subagents` — sub-agent patterns, double-review, parallelization, independence
- `performance` — benchmarks, measurement, sustained, compute/memory isolation
- `other` — miscellaneous (reports, output paths, transparency, etc.)

To add a new domain, edit `INDEX.md` (add a section) AND this README.

## Manual Editing

Allowed (and even encouraged) for:
- Refining `applies_when` / `do_not_apply_when` after a failed Alpha retrieval
- Archiving an obsolete lesson (`status: archived`)
- Merging two duplicate lessons

Keep a trace in the commit message.
