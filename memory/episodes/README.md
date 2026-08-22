# `memory/episodes/` — Trace of each /commontrace run

## Role

An episode file `YYYY-MM-DD_<slug>.md` is written by the sub-agent **Omega** at **Phase 10** of each /commontrace run (unless `--skip-omega`). Writing is **systematic** (traceability), regardless of the final verdict or whether new lessons are created.

## Workflow (v2.2)

1. /commontrace run proceeds (Phases 0-8).
2. Phase 9: orchestrator produces the mini-retro.
3. Phase 10: Omega receives everything (task, Alpha, A, B, verdict, retro) and writes the episode HERE.
4. Phase 11: Lambda audits Omega proposals (ACCEPTED | REJECTED | NEEDS REFINEMENT). The orchestrator applies the ACCEPTED ones and updates the `lessons_validated_by_lambda` field in this episode's frontmatter with the effectively validated slugs.

Note v2.2: the frontmatter field `lessons_validated_by_user` has been renamed to `lessons_validated_by_lambda` to reflect the switch to automatic Lambda validation (100% automated workflow, no longer user-dependent).

## Format

Strict YAML frontmatter (parsable by `yaml.safe_load`), followed by a markdown body with fixed sections.

See `episode_template.md` for an empty skeleton. Required fields:

| Field | Type | Description |
|---|---|---|
| `name` | string | `YYYY-MM-DD_<slug>`, identical to the file name (without .md) |
| `description` | string | 1-line summary of the run |
| `task_invocation` | string | Verbatim of the `/commontrace ...` invocation |
| `tags` | list[string] | Free tags for Alpha pre-filtering (e.g. `[cuda, refactor]`) |
| `project` | string | Project detected from the cwd (e.g. `<your_project>`) — used for the `transfer_gap` metric |
| `verdict` | enum | `CONFORM` \| `ARBITRATION` \| `ABANDON` |
| `importance` | int | Integer 1-5 REQUIRED — see full rubric in `SKILL.md` section "Importance rubric" (5=showstopper, 4=critical, 3=useful, 2=minor, 1=anecdotal). Set by Omega when writing the episode. |
| `importance_rationale` | string | 1-sentence concrete REQUIRED justification for the score (not generic). |
| `n_iterations` | int | Number of A+B iterations performed |
| `commit_sha` | string | SHA of the final commit |
| `duration_minutes` | int | Total duration of the run |
| `lessons_retrieved_by_alpha` | list[string] | Slugs of lessons formally selected by Alpha in its "Applicable lessons" block. Does NOT include counter-examples mentioned in "Mandate reminder" — for those see the "Lessons consulted" block in the Alpha report. |
| `lessons_hit` | list[string] | Slugs of lessons actually useful (according to A/B reports + orchestrator retro). **Not bounded by `retrieved`**: can include background-active lessons (counter-examples, implicit rules). Benchmark computes strict (hit ∩ retrieved / retrieved) and permissive (hit / retrieved). |
| `lessons_proposed_by_omega` | list[string] | Slugs of new lessons proposed by Omega (before Lambda validation) |
| `lessons_validated_by_lambda` | list[string] | Slugs effectively validated by Lambda in Phase 11 and applied by the orchestrator — filled AFTER the fact. Renamed in v2.2 from `lessons_validated_by_user` (switch to automatic validation). |

### Omega Criteria for Proposing a New Lesson from an Episode (v2.1)

Omega proposes a candidate lesson if at least ONE of the following two criteria is true (in addition to "not covered by an existing lesson" AND "generalizable cross-project"):

- **(A)** Source episode importance >= 3 AND generalizable cross-project.
- **(B)** Importance 4-5 even on a single occurrence — a showstopper / critical issue deserves to be captured immediately, without waiting for a second occurrence.

Replaces the old criterion ">= 2 episodes showing it" (too strict for rare but critical showstoppers).

## Why These Fields

The `lessons_*` fields serve to:
- Trace what Alpha retrieved and what actually helped (audit `lessons_retrieved_by_alpha` vs `lessons_hit`).
- Trace the Omega proposal -> Lambda validation chain (`lessons_proposed_by_omega` vs `lessons_validated_by_lambda`).
- Enable future cross-project analyses via the `project` field.

A benchmark script measuring `lesson_quality` / `implicit_retrieval` / `transfer_gap` will be added in a separate step and will rely on these fields.

## Body Sections

- **What happened**: 5-10 factual lines (what was done, how, result).
- **What surprised me**: verbatim extract from the orchestrator retro (Phase 9).
- **What worked well**: 0-N items.
- **What worked less well**: 0-N items.

## Manual Editing

Except in exceptional cases (correcting an erroneous field, adding a post-mortem), **do not edit** an episode after its creation. It is an **archive**, not a living document. Corrections should go through a new episode or a dedicated documented commit.
