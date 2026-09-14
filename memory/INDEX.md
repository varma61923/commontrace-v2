# Memory Index — agent_type: code

Hierarchical index by domain of the `/commontrace` skill memory base (lessons + episodes). Edited by the orchestrator in Phase 11 (after automatic Lambda validation of Omega proposals).

## Usage Conventions

- **Who edits**: the `/commontrace` orchestrator in Phase 11 (after automatic Lambda validation). Manual editing allowed to refine / archive / merge (keep a trace in the commit message).
- **When**: on each creation / update / revision of a lesson validated by Lambda (Phase 11 auto), and on each episode write (mention in the domain's Episodes section).
- **How to add a new domain category**: edit this file (add a `### <Domain>` section), edit `memory/lessons/README.md` (add the mention in the hierarchical list), and use this domain in the `domain:` field of the relevant lessons' frontmatter.

## Line Format

```
- [slug](relative/path/to/file.md) — rule in 1 sentence | tags: [a,b,c] | importance: N | uses: N | last_hit: YYYY-MM-DD or NEVER
```

The `importance` field is an integer 1-5 (full rubric in `SKILL.md` section "Importance rubric"). Single source of truth = the `lesson_<slug>.md` file itself; this INDEX.md reflects it. For lines without `importance` (episodes/lessons prior to v2.1), Alpha applies default 3 and flags "to be calibrated".

## Sections by Domain

> The entries below are **fictitious illustrative examples** (prefix
> `example`) provided to show the format in action. Delete them once your
> own lessons/episodes accumulate. Planned domains: Git/Safety, CUDA/GPU,
> Refactor, Testing, Subagents, Performance, Other.

### Testing

#### Lessons
- [lesson_example_regression_test_before_refactor](lessons/lesson_example_regression_test_before_refactor.md) — Write a regression test capturing current behavior BEFORE the refactor, not after | tags: [testing, refactor, regression, example] | importance: 4 | uses: 1 | last_hit: 2026-07-01

#### Episodes
- [2026-07-01_example-api-pagination](episodes/2026-07-01_example-api-pagination.md) — Added cursor-based pagination to GET /items via the A+B pattern (CONFORM / 1 iteration)

---

### Subagents

#### Lessons
- [lesson_example_serialize_subagents_same_file](lessons/lesson_example_serialize_subagents_same_file.md) — Serialize two sub-agents editing the same file to avoid silent overwrite | tags: [subagents, orchestration, concurrency, example] | importance: 5 | uses: 0 | last_hit: NEVER

#### Episodes
*(see the `2026-07-01_example-api-pagination` episode in Testing — it is the one that surfaced this lesson in Phase 10/11)*
