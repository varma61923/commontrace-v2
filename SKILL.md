---
name: commontrace
description: "A+B double-review pattern with sub-agents, looping until conformity (max 3 iterations, then orchestrator arbitration). Spawns sub-agent A (implementer) + immediate commit + independent sub-agent B (reviewer), and iterates on gaps. Carries long-term memory across runs: Alpha retrieves relevant lessons before work starts, Omega proposes new lessons from what happened, and Lambda validates the backlog independently, with a semantic attention pre-filter so retrieval scales past 100+ lessons. Ideal for architectural code, heavy refactors, CUDA/GPU ports, and critical fixes where an independent review adds value. Invocation: `/commontrace <task description + inline success criteria>`. Triggers: `/commontrace`, `do double review`, `commontrace this task`, `launch A+B on ...`."
---

# Skill `/commontrace` — A+B double-review pattern with loop + long-term learning

Delegates a task to an **A implementer** sub-agent, then an independent **B reviewer** sub-agent, with **immediate commit after A** and an **iteration loop on gaps** (max 3 iterations, then orchestrator arbitration).

## Platform Mapping

This spec uses generic terms. Here is how they map to specific agent platforms:

| Generic concept | Claude Code | Devin | Other agents |
|---|---|---|---|
| Spawn sub-agent async | `Agent(subagent_type='general-purpose', run_in_background=true)` | `run_subagent(is_background=true)` | Platform-specific sub-agent spawn |
| Prompt user | `AskUserQuestion` | `ask_user_question` | Platform-specific user prompt |
| Task tracker create | `TaskCreate` | `todo_write` | Any task list / tracker |
| Task tracker update | `TaskUpdate` | `todo_write` (update status) | Any task list / tracker |
| Read file | `Read` | `read` | File read API |
| Edit file | `Edit` | `edit` | File edit API |
| Run command | `Bash` | `exec` | Shell/command API |

**v2 (2026-05-26)**: added a long-term learning mechanism via **Alpha (retrieval)** and **Omega (synthesis + memory enrichment proposals)** sub-agents, and a hierarchically structured memory base (`memory/`). This mechanism is an experimentation ground for the CommonTrace fleet-learning platform.

**v2.2 (2026-05-27)**: Phase 11 fully automated via the **Lambda** sub-agent (independent reviewer of the memory backlog). Lambda audits each Omega proposal against 4 criteria (formal quality, non-duplicate, generalization, importance calibration) and returns a verdict: ACCEPTED / REJECTED / NEEDS REFINEMENT. The orchestrator applies ACCEPTED entries without any human in the loop. Allows an agent to execute the complete pipeline with no user dependency.

## Pipeline diagram (v2.2)

```
Phase 0  → Alpha (memory retrieval) ──────────────┐
                                                   ↓
Phase 1  → Parse invocation + completion          (brief A enriched by Alpha)
Phase 2  → Create task in the task tracker
Phase 3  → Sub-agent A (implementer)               ──→ report A
Phase 4  → Immediate commit after A
Phase 5  → Sub-agent B (independent reviewer)       ──→ verdict B
Phase 6  → Decision based on verdict B
            ├─ CONFORM → continue to Phase 9
            └─ GAP
                ├─ it < max → Phase 7 (iteration A2..An, back to Phase 4)
                └─ it ≥ max → Phase 8 (orchestrator arbitration)
Phase 7  → Iteration (relaunch A with brief enriched by B's gaps)
Phase 8  → Orchestrator arbitration (max_iterations reached)
                                                   ↓
Phase 9  → Mini-retro by orchestrator (3 questions)
Phase 10 → Omega (synthesis + lesson proposals)    ──→ writes episode/, proposes lessons
Phase 11 → Lambda (automatic backlog validation)   ──→ ACCEPTED/REJECTED/NEEDS REFINEMENT verdicts, orchestrator applies ACCEPTED
```

## When to use

- **Architectural code**: refactor, port, redesign (e.g. CUDA port, algorithmic scan)
- **Risky task** where an independent review adds value (e.g. modifying a detector, critical bug fix)
- **Task with measurable success criteria**: tests, empirical parity, perf, anti-patterns

**DO NOT use for**:
- Trivial tasks (1-2 files, < 20 lines modified) — see "Main session bypass" below
- Exploratory tasks without clear criteria
- Pure read/research tasks (use an exploratory or general-purpose sub-agent)
- Multi-file refactors with conflicts (cf. `feedback_parallel_subagents_file_overlap`)

## Main session bypass (trivial tasks < 20 lines)

For tasks whose measurable scope is **< 20 lines modified and < 3 files touched**, the main session (Orchestrator) can handle them directly **WITHOUT** launching the A+B pattern (Phase 0 Alpha → Phase 10/11 Omega/Lambda). Avoids unjustified overhead on simple tasks.

**Bypass eligibility criteria**:
- Simple modification: rename, fix typo, add Pydantic literal, bump version, 5-line bug fix, add targeted test
- Existing tests cover the change (no new test needed, or a single trivial test addition)
- No new business logic
- No public API refactor
- No invariant modifications

**Bypass workflow (direct main session)**:
1. Orchestrator reads the affected files
2. Presents the diff to the user (open chat, interactive section-by-section validation — cf. `lesson_show_changes_before_editing`)
3. Applies the edit after user OK
4. Runs existing tests to confirm no regression
5. Targeted Git commit with explicit message

**DO NOT bypass (use full /commontrace)**:
- Architectural refactor (e.g. API change, new pattern)
- CUDA / GPU port
- Public API modification
- Critical fix on production code
- Anything touching > 20 lines or > 3 files
- Task with multi-dimensional measurable success criteria

**Note**: this section added 2026-05-28 following the dreamer session OGHAM §4.4 (over-engineering observed on simple fixes like `midnight_ny` 5 lines or T4 magic numbers neutralization). The recent empirical bypass pattern (e.g. midnight_ny Fix D 2026-05-28 handled in main session without /commontrace) demonstrated its effectiveness.

## Workflow

### Phase 0 — Alpha (memory retrieval)

**Objective**: before coding, read the `memory/` base to identify past lessons and episodes applicable to the incoming task. Inject the result into the A brief.

Spawn a fresh sub-agent **Alpha** asynchronously. Alpha is **read-only** on memory — it does not touch anything else.

**If a randomized holdout is running, Alpha honours it.** This is the
only way anyone — including you — can establish that this memory pipeline
helps rather than merely correlates with success; a lesson that fires on
the hardest tasks looks good by retrieval count and may be making outcomes
worse. Retrieval is where the decision belongs, because it is the only
point that knows which lessons were *eligible*, and eligibility is what
makes the later comparison causal rather than confounded.

- **Local memory, over MCP:** if the `commontrace-local` server is attached,
  `retrieve(task, occasion_id="<id>")` withholds and logs in one step and
  returns the withheld lessons under `withheld`; `capture(occasion_id="<id>",
  resolved=...)` joins the outcome back. No shell needed.
- **Local memory, from a shell:** `commontrace query --experiment
  --occasion-id <id>` does the same, and `commontrace capture --occasion-id
  <id>` joins the outcome back. Both surfaces share one arm-assignment
  implementation, so an occasion gets the same arm either way.
- **Hub memory:** pass `occasion_id` to `search_traces`. If an experiment
  is running the response carries `holdout.withhold`; **exclude those
  traces from the brief**. After the task, call
  `record_occasion_outcome(occasion_id, succeeded)`.

Using a withheld lesson anyway does not raise an error. It silently moves
that occasion into the treated arm, biasing the measured effect toward
zero — so this is a rule Alpha has to follow deliberately, not one the
tooling can enforce afterwards. Read the result with
`commontrace prove outcomes` (Hub) or `commontrace experiment` (local); a
lesson coming back as `HURTS` is the point, not a failure.

**Alpha brief (self-contained template, copy verbatim)**:

```
You are Alpha, a memory-retrieval sub-agent for /commontrace. You operate in read-only mode.

## Strict mandate
- READ the memory base at `$COMMONTRACE_ROOT/memory/`
- IDENTIFY lessons and episodes relevant to the incoming task
- RETURN a structured brief that will be injected verbatim into the A brief

You do NOT touch anything other than reading memory. No file writes, no edits, no git.

## Incoming task (user verbatim)
[VERBATIM /commontrace INVOCATION HERE]

## Mandatory workflow

### Step 0 — Semantic attention pre-filter (v2.3)
RUN via shell:
```
python3 $COMMONTRACE_ROOT/memory/attention/query.py "[verbatim /commontrace invocation, task + criteria]" --top-k=10 --include-importance-floor=4
```
Read the output (one line per lesson, format `slug | cosine=0.XXX | importance=N`). These are your priority candidates for steps 1-2 below. The override `--include-importance-floor=4` guarantees that all lessons with `importance >= 4` are present in the output, even if absent from the top-K cosine — you MUST keep them as candidates to consider (cf. step 7).

The cosine score is a COMPLEMENT to qualitative sorting (importance × tag_match), not a replacement. You always judge `applies_when` / `do_not_apply_when` at step 4. If the script fails (index.npz file missing, Python error), continue with the classic workflow (steps 1-8) and note the failure in the report.

### Steps 1-8 (classic qualitative workflow)
1. READ `memory/INDEX.md` to verify pre-filter candidate relevance and complement by domain if needed
2. Select 3-7 candidate lessons/episodes — prioritize the top-K cosine from step 0, complemented by `importance >= 4` candidates not already covered
3. READ the file for each candidate (YAML frontmatter + body)
4. Verify `applies_when` and `do_not_apply_when` of each against the incoming task
5. Retain ONLY those that pass this semantic filter
6. **Sort retained lessons by descending score**: `score = importance × tag_match`
   - `importance` = frontmatter field (integer 1-5; default 3 if absent — flag "needs calibration" in the report)
   - `tag_match` = number of lesson tags present in the incoming task keywords (simple proxy, integer ≥ 0)
7. **Importance safety net**: any lesson with `importance >= 4` must be considered even if `tag_match == 0`, because it represents a potentially cross-cutting critical/showstopper risk — do NOT filter it out due to tag match failure; mention it with note "high importance, applicability to validate"
8. Synthesize in the output format below

## EXACT output format (follow scrupulously)

## RETRIEVED MEMORY (Alpha)

### Applicable lessons (3-5 max, sorted by importance × tag_match descending)
1. **[lesson_slug]** (cosine: 0.XX, importance: N) — rule in 1 sentence
   - Why this applies here: [1 sentence anchored in the incoming task, not generic]
   - How to apply: [concrete action in the code to produce — what A must do/avoid]

2. **[lesson_slug]** (cosine: 0.XX, importance: N) — ...

Format note: `cosine: 0.XX` is the score returned by `query.py` at step 0 (put `N/A` if the lesson was surfaced only via the importance ≥ 4 safety override without being in the top-K cosine). The requirement "How to apply specific per lesson" is PRESERVED — not a copy-paste of the lesson source, but a concrete action anchored in the incoming task.

### Similar previous episodes (0-2)
- [episode_slug] (importance: N) — 1-sentence summary + what was retained from it

### Recommendations for the A brief
- To add in the "Anti-pattern constraints" section: ...
- To add in the "Documents to read" section: ...

### Confidence
HIGH | MEDIUM | LOW | NONE

### Withheld by the holdout (if an experiment is running)
[Slugs/trace ids the holdout told you NOT to use on this occasion, and therefore
absent from "Applicable lessons" above. Empty when no experiment is running.]

### Lessons consulted (for traceability)
[Complete list of slugs consulted, even those not selected — useful for retrieval audit. Mention importance in parentheses, cosine if available, and flag "needs calibration" if importance is absent.]

## If NO applicable precedent
Return the block below EXACTLY:

## RETRIEVED MEMORY (Alpha)

### Confidence
NONE

### Lessons consulted
[list of slugs read anyway, with cosine if available]

### Message
No applicable precedent. Task in uncharted territory — orchestrator, exercise increased caution.

GO.
```

**Failure or confidence NONE = NON-BLOCKING**:
- If Alpha fails (timeout, error), the orchestrator continues without blocking.
- If Alpha returns `Confidence: NONE`, the orchestrator continues with an explicit signal in the A brief: `"## RETRIEVED MEMORY (Alpha)\nno memory used — task in uncharted territory"`.

**Skippable** via `--skip-alpha` (cf. parameters). If `--alpha-only`, Alpha runs alone and the orchestrator stops after displaying the report (useful for testing retrieval without coding).

### Phase 1 — Parse the invocation and complete if needed

The user invokes `/commontrace <task>`. The task may include:
- Task description
- Explicit success criteria
- Files to touch / not touch
- Specific constraints

**If the task is ambiguous** or success criteria are missing: prompt the user for 1-2 clarifying questions BEFORE launching A. Standard criteria to validate:
- Tests to pass (which ones?)
- Semantics to preserve (which ones?)
- Files NOT to touch (territories of other parallel sub-agents?)
- Performance / empirical parity required?

**If the task is sufficiently clear**: proceed directly to Phase 2.

### Phase 2 — Create the task in the task tracker

Create a task entry in the orchestrator's task tracker with subject = task summary, description = success criteria, status = `in_progress`.

### Phase 3 — Launch sub-agent A (implementer)

Spawn a fresh sub-agent A asynchronously.

**A brief (template, enriched by Alpha output if Phase 0 ran)**:

```
You are A, an implementer sub-agent for [TASK]. An independent reviewer sub-agent B will audit your output. Pattern: feedback_subagent_double_review.

[VERBATIM INSERTION OF ALPHA OUTPUT HERE — complete "## RETRIEVED MEMORY (Alpha)" block]

## Mission
[Precise task description]

## Success criteria (user verbatim)
- [Criterion 1]
- [Criterion 2]
- ...

## Documents to read before coding
- [Project canonical docs list]
- [Spec / requirements doc if applicable]
- [+ Docs recommended by Alpha if applicable]

## Anti-pattern constraints (standard)
- No alias @property / back-compat (strict rename if refactor)
- No magic number without empirical justification
- No mock / separate simulation
- No output to /tmp (use results/<run_name>/)
- No avoidable Python loop if vectorizable
- No tests skip / xfail to mask a bug
- Project semantics preserved (existing tests must pass)
- [+ Anti-patterns recommended by Alpha if applicable]

## DO NOT
- DO NOT commit (the orchestrator will do it)
- DO NOT perform git operations (stash, clean, restore) — risk of code loss
- DO NOT touch files: [exclusion list]

## Final report format
[Expected structure: modified files, tests, measurements, surprises]

## CWD / Python
[Project standard]

GO. Aim for CONFORM in 1 iteration.
```

Wait for the completion notification. **No polling.**

### Phase 4 — Immediate commit after A

**As soon as A finishes**, without waiting for B:
1. Read the A report (modified files)
2. `git add` the files specified by A
3. `git status` to verify
4. `git commit` with a clear message including:
   - Initial task
   - A verdict (synthetic report)
   - Passing tests
   - Current iteration (if > 1)

**Justification**: avoids code loss in case of involuntary git operation (lived incident: sub-agent B0a v3 ran git clean which erased cuda_v3/*.py).

If A signals a blocker / abandonment: do not commit, return to the user.

### Phase 5 — Launch sub-agent B (independent reviewer)

Spawn a fresh sub-agent B asynchronously.

**B brief (template)**:

```
You are B, an independent reviewer sub-agent for [TASK]. You are NOT the code author. You judge against the success criteria. Pattern: feedback_subagent_double_review.

## Strict mandate
1. READ the code to review (diff from commit [SHA])
2. READ the requirements doc / spec / success criteria
3. Audit methodologically (semantics, tests, anti-patterns, user criteria)
4. Verify empirically when possible (run pytest, mini-bench)
5. Issue verdict: CONFORM or GAP
6. DO NOT modify the code (read-only)
7. DO NOT perform git operations (stash, clean, restore, reset, checkout)

## Files to review
[Precise list: A's modifications + new files, from git diff HEAD~1]

## Success criteria (user verbatim)
- [Criterion 1]
- [Criterion 2]
- ...

## Audit grid
### Semantics preserved (CRITICAL)
- Existing tests pass? Run pytest and confirm
- Project semantics: verify by diff reading + execution

### Anti-patterns
- Alias @property present?
- Magic number without justification?
- Output to /tmp?
- Avoidable Python loop?
- Tests skip / xfail?

### User criteria
- For each criterion: PASS / FAIL with precise citation

### Empirical tests
- Run relevant commands (pytest, bench, smoke)
- Report numerical results

## Verdict format (CRUCIAL)

```
## VERDICT B

**Decision**: [CONFORM | GAP]

**If CONFORM**:
- Validated notable points (3-5)
- Optional non-blocking recommendations

**If GAP**:
- Numbered list of gaps:
  1. [section] [file:line] [requirement] [observed] [correction]
  2. ...
- Severity: BLOCKING / MAJOR / MINOR

**Tests run**: commands + results
```

## CWD / Python
[Project standard]

GO. Rigorous audit, factual verdict. Do not rewrite the code.
```

Wait for the completion notification. **No polling.**

### Phase 6 — Decision based on verdict B

**If CONFORM**:
1. Mark the task as completed in the tracker
2. Output a summary to the user (3-5 lines):
   - Task completed
   - B verdict
   - Commit SHA
   - Key tests / measurements
3. **Continue to Phase 9** (orchestrator mini-retro, unless `--skip-omega`).

**If GAP**:
- If iteration < `max_iterations` (default 3) → **Phase 7: iterate**
- If iteration ≥ `max_iterations` → **Phase 8: orchestrator arbitration**

### Phase 7 — Iteration (relaunch A with enriched brief)

Generate a **new A brief** (A2, A3, ...) with:
- Original brief (including Alpha output from Phase 0 if it ran)
- Additional section: **"B gaps from previous iteration to fix"**
  - Numbered list verbatim from B
  - Severity preserved
- Instruction: "Fix the gaps listed above while preserving the initial success criteria."

Spawn sub-agent A2 (asynchronously).

**Loop**: back to Phase 4 (immediate commit) → Phase 5 (new B) → Phase 6 (verdict).

### Phase 8 — Orchestrator arbitration (max_iterations reached)

If after 3 iterations still GAP:
1. **Read the persistent gaps** identified by B
2. **Targeted reading** of the code (read the affected files)
3. **Decide**:
   - Either fix the residual gaps yourself (if minor) then commit
   - Or present to the user: "After 3 A+B iterations, residual gaps: [list]. Recommendation: [option A / option B / abandon]."
4. Mark the task as completed in the tracker with note "orchestrator arbitration"

**Continue to Phase 9** (orchestrator mini-retro).

### Phase 9 — Orchestrator mini-retro (before Omega)

**Objective**: capture 30 seconds of meta-reflection on the run that just completed, to feed Omega with a qualitative signal that the A and B reports do not contain (orchestrator impressions, surprises, actual usefulness of Alpha).

The orchestrator produces this retro **by default on its own** (self-reflection in output). For sensitive runs (e.g. highly ambiguous tasks, persistent A↔B conflicts, user present), the orchestrator **may** ask the user for confirmation/correction by prompting for input (max 3 questions).

**EXACT retro block** (produce verbatim):

```
## Orchestrator retro (before Omega)

1. What went well: [short sentence]
2. What surprised / bothered me: [short sentence]
3. Was the Alpha brief useful? [YES / PARTIALLY / NO — why]
4. What did we learn about THE MARKET / project domain (vs meta-workflow)? [short sentence or "nothing notable domain-wise this run"]
```

This block is injected verbatim into the Omega brief at Phase 10.

**Note on the 4th question (added 2026-05-28)**: countermeasure to the meta-workflow magnifying-glass effect observed during the first OGHAM runs (cf. dreamer session 2026-05-27_first-dreamer-ogham §1.6) — the majority of generated lessons were meta (`amend_brief_a`, `orchestrator_fix_residual`, etc.) at the expense of domain lessons (`signal_absence`, `prefer_scalar`). Forcing explicit domain reflection rebalances the long-term memory base. Answer "nothing notable" is OK and explicit — no hallucination.

**Skippable together with Phase 10/11** via `--skip-omega`.

### Phase 10 — Omega (synthesis + memory enrichment proposals)

**Objective**: systematically write the episode (run traceability) and **propose** (without writing) 0-N candidate lessons and 0-N updates to existing lessons.

Spawn a fresh sub-agent **Omega** asynchronously.

**Omega brief (self-contained template, copy verbatim)**:

```
You are Omega, a synthesis + memory enrichment sub-agent for /commontrace.

## Mandate
1. WRITE the EPISODE for the run that just completed (ALWAYS, traceability is mandatory) directly in `memory/episodes/YYYY-MM-DD_slug.md`; CALIBRATE the episode importance (1-5 + 1-sentence rationale) per the SKILL.md rubric
2. PROPOSE (DO NOT WRITE) 0-N new candidate lessons, with importance + 1-sentence rationale for each
3. PROPOSE (DO NOT WRITE) 0-N updates to existing lessons
4. Optionally: flag "NEEDS REVISION" if a lesson retrieved by Alpha proved non-applicable

You do NOT have authorization to write in `memory/lessons/`. Lesson files are written by the orchestrator AFTER automatic Lambda validation (Phase 11).

## Importance rubric (1-5, apply to both episodes AND lessons)

- **5 (showstopper)**: without this lesson, the entire task class fails or causes data loss / security breach.
- **4 (critical)**: ignoring this lesson → high probability of major rework or a subtle hard-to-detect bug.
- **3 (useful)**: the lesson avoids a common anti-pattern or methodological trap. Saves significant time.
- **2 (minor)**: valid lesson but limited impact, applicable to a specific sub-case.
- **1 (anecdotal)**: interesting observation but not very actionable → prefer documenting as an episode note.

The rationale is MANDATORY and must be 1 concrete sentence (not "important because useful").

## Inputs (verbatim)

### Initial task (/commontrace invocation)
[VERBATIM INSERTION HERE]

### Alpha report (Phase 0)
[VERBATIM INSERTION HERE — or "NONE (--skip-alpha)" if skipped]

### Initial A brief
[VERBATIM INSERTION HERE]

### A1..An reports (all iterations)
[VERBATIM INSERTION HERE, separated by "--- ITERATION N ---"]

### B1..Bn reports (all iterations)
[VERBATIM INSERTION HERE, separated by "--- ITERATION N ---"]

### Final verdict
[CONFORM after N iterations | ORCHESTRATOR ARBITRATION after 3 iterations | ABANDON]

### Orchestrator retro (Phase 9)
[VERBATIM INSERTION OF THE BLOCK]

### Run metadata
- task_invocation: [verbatim]
- project: [detected from cwd; tag the SUB-PROJECT distinctly, not just the parent repo — e.g.
  "module-a" or "module-b", not a single "<your_project>" collapsing every sub-project run
  together. Collapsing sub-projects under one name makes transfer_gap mechanically 0% forever
  (see benchmark/STATUS.md §2.4 and §4.2) because no hit can ever look "cross-project" if
  everything shares the same project tag. Only fall back to the bare repo name when the run
  genuinely isn't scoped to any sub-project.]
- commit_sha: [final SHA]
- duration_minutes: [N]
- n_iterations: [N]

## Mission 1 — Write the episode (ALWAYS)

File: `memory/episodes/YYYY-MM-DD_slug.md` where:
- YYYY-MM-DD = run date
- slug = 3-5 words derived from the task (lowercase, separator `-`)

STRICT YAML frontmatter (parsable by yaml.safe_load):

---
name: YYYY-MM-DD_slug
description: one-line summary of the run
task_invocation: verbatim invocation /commontrace ...
tags: [tag1, tag2]
project: project-name   # tag the SUB-PROJECT distinctly (e.g. "module-a", "module-b"), not
                         # one shared parent-repo name for every sub-project run -- see the
                         # "Run metadata" note above and benchmark/STATUS.md §2.4/§4.2
verdict: CONFORM | ARBITRATION | ABANDON
importance: N          # integer 1-5, see rubric above
importance_rationale: "1-sentence concrete, justifies the score"
n_iterations: N
commit_sha: xxx
duration_minutes: N
lessons_retrieved_by_alpha: [list of lesson slugs returned by Alpha]
lessons_hit: [list of lesson slugs actually useful based on the run + retro]
lessons_proposed_by_omega: [list of proposed new lesson slugs below]
lessons_validated_by_lambda: []  # to be filled by orchestrator in Phase 11 (post-Lambda)
---

## What happened
[5-10 factual lines: what we did, how, result]

## What surprised me
[Extract from orchestrator retro — verbatim from Phase 9]

## What worked well
[List 0-N items, based on A/B reports + retro]

## What worked less well
[List 0-N items]

## Mission 2 — Propose new lessons (0-N)

Criteria for PROPOSING a new lesson (at least ONE of the two must be TRUE, AND the lesson must not be covered by an existing one AND must be generalizable beyond this specific project):
- **(A)** Source episode importance ≥ 3 AND generalizable beyond this specific project (check memory/INDEX.md + memory/lessons/ to verify non-duplicate)
- **(B)** Importance 4-5 even on a single occurrence — a showstopper / critical item deserves to be captured immediately, no need to wait for a second occurrence

The candidate lesson importance is derived from its `source_episodes` (max or average of source importances). You can adjust it by +/- 1 when proposing (justify in the proposal).

If nothing notable: state frankly "Nothing new to learn, episode archived for traceability".

## Mission 3 — Propose updates to existing lessons (0-N)

For each lesson retrieved by Alpha that actually helped:
- Propose: `uses += 1`, `last_hit = today`, append `source_episodes`

For each retrieved lesson that proved non-applicable / poorly formulated:
- Propose: flag "NEEDS REVISION" with reason

## EXACT output format (verbatim)

## OMEGA OUTPUT

### Episode written
- Path: memory/episodes/YYYY-MM-DD_slug.md
- Status: created
- Importance: N — "[1-sentence rationale]"

### Candidate lessons (to be validated by Lambda BEFORE write)
1. **[proposed_lesson_slug]**
   - Rule: ...
   - Why: ... (cite the source episode)
   - How to apply: ...
   - applies_when: ...
   - do_not_apply_when: ...
   - Importance: N — "[1-sentence concrete rationale]" (derived from source_episodes, adjusted if relevant)
   - Justification "why new vs existing": ...

2. ...

### Lesson updates (existing lessons to increment)
1. [existing_lesson_slug]: +1 uses (helped on this episode), append source_episode YYYY-MM-DD_slug
2. ...

### Lesson revisions (existing lessons to flag)
1. [existing_lesson_slug]: NEEDS REVISION — concrete reason
2. ...

### No new lessons?
[If nothing notable, state it frankly and explain why the run did not generate transferable learning]

GO.
```

### Phase 11 — Lambda (automatic memory backlog validation)

**Objective**: automatically audit each Omega proposal (new lessons, updates, revisions) via an independent **Lambda reviewer** sub-agent, then the orchestrator applies only ACCEPTED proposals. 100% automated workflow, usable by an agent without any human in the loop.

**Lambda is to Omega what B is to A**: an independent reviewer who judges against explicit criteria, not the author of the proposals.

**Workflow**:
1. The orchestrator spawns Lambda asynchronously as a fresh sub-agent with the verbatim brief below, injecting the Omega output + read access to the memory base.
2. Lambda audits each proposal against 4 criteria (formal quality, non-duplicate, generalization, importance calibration) and returns an ACCEPTED / REJECTED / NEEDS REFINEMENT verdict per proposal.
3. The orchestrator applies ACCEPTED proposals (writes lessons / updates / revisions).
4. REJECTED and NEEDS REFINEMENT entries are logged in the final report for traceability (not applied).

**Lambda brief (self-contained template, copy verbatim)**:

```
You are Lambda, an independent memory backlog reviewer sub-agent for /commontrace. Independent of Omega's choices.

## Strict mandate
- READ the memory base at `$COMMONTRACE_ROOT/memory/` (existing lessons, INDEX.md, recent episodes if needed)
- READ the importance rubric in `SKILL.md` section "Importance rubric"
- READ the Omega report (verbatim injected below)
- AUDIT each Omega proposal (new lessons + updates + revisions) against 4 criteria
- RETURN an ACCEPTED | REJECTED | NEEDS REFINEMENT verdict per proposal with 2-3 sentence justification

You do NOT have authorization to write in `memory/`. You are strictly read-only. No file writes, no edits, no git. The orchestrator applies your ACCEPTED verdicts.

## Inputs (verbatim)

### Omega report
[VERBATIM INSERTION HERE — complete "## OMEGA OUTPUT" block]

### Expected lesson format (reminder)
- Strict YAML frontmatter: name, description, tags, domain, importance (1-5), importance_rationale, importance_history ([]), applies_when, do_not_apply_when, uses, last_hit, source_episodes, status (active|review|archived)
- Body: ## Rule, ## Why, ## How to apply, ## Counter-examples
- Reference: `memory/lessons/README.md` and `memory/lessons/lesson_template.md`

### Importance rubric (verbatim reminder)
- 5 (showstopper): without this lesson, the entire task class fails or causes data loss / security breach.
- 4 (critical):    ignoring this lesson → high probability of major rework or a subtle hard-to-detect bug.
- 3 (useful):      the lesson avoids a common anti-pattern or methodological trap. Saves significant time.
- 2 (minor):       valid lesson but limited impact, applicable to a specific sub-case.
- 1 (anecdotal):   interesting observation but not very actionable → prefer documenting as an episode note.

## Audit workflow (per proposal)

### For each NEW LESSON proposed

Verify the following 4 criteria. ACCEPTED verdict only if all 4 pass.

1. **Formal quality**:
   - `applies_when` concrete and precise (not "when refactoring" but "when doing an architectural refactor touching ≥ 3 files")
   - `do_not_apply_when` explicit (not "except special cases")
   - `importance` (1-5) AND `importance_rationale` (1-sentence concrete, actionable, not "important because useful")
   - Proposed YAML valid (required fields present, correct types)

2. **Non-duplicate**:
   - READ `memory/INDEX.md` for the relevant domain
   - Semantic grep: does the proposed Rule overlap with an existing lesson (same domain + similar tags)?
   - If overlap → propose UPDATE of the existing lesson instead of a new one. Verdict: REJECTED or NEEDS REFINEMENT with note "replace with UPDATE of lesson X"

3. **Generalization**:
   - Can you imagine ≥ 3 application contexts outside the current project/run? (e.g. other project, other stack, other task type)
   - If too specific → REJECTED with note "too specific to run X, better suited as an episode note"

4. **Importance calibration**:
   - Is the score defensible against the 1-5 rubric?
   - If A and B from the run diverged on calibration (cf. A/B reports injected in the Omega brief), judge the gap: ±1 acceptable, ≥2 → NEEDS REFINEMENT with note "calibration gap to arbitrate"

### For each proposed UPDATE (uses += 1, last_hit, etc.)

1. **Consistency**:
   - Proposed `source_episode` not already present in the lesson's `source_episodes` (otherwise double-counting → REJECTED)
   - Proposed `last_hit` ≤ today's date (no future date → REJECTED)
   - Proposed `uses` consistent with current `uses` + 1 (otherwise REJECTED)

2. **Justification**:
   - Was the lesson actually useful in the run? (verify in the Omega report that the slug is in the episode's `lessons_hit`)
   - If not confirmable → REJECTED with note "lesson_hit not confirmed by report"

### For each proposed REVISION (status active → review)

1. **Documented reason**:
   - Is the reason (rationale for the NEEDS REVISION flag) documented concretely (citation from A/B report or orchestrator retro)?
   - If generic or unsourced reason → REJECTED

## EXACT output format (verbatim)

## LAMBDA OUTPUT

### Decisions per proposal

#### New lesson [proposed_lesson_slug]
- **Decision**: ACCEPTED | REJECTED | NEEDS REFINEMENT
- **Justification**: [2-3 sentences covering formal quality, non-duplicate, generalization, importance calibration]
- **If NEEDS REFINEMENT**: precise fields to correct

#### Update [existing_lesson_slug]
- **Decision**: ACCEPTED | REJECTED
- **Justification**: [verify source_episode not already present, dates consistent, uses consistent, lesson_hit confirmed]

#### Revision [existing_lesson_slug]
- **Decision**: ACCEPTED | REJECTED
- **Justification**: [reason documented in source episode?]

### Summary
- Total proposals: N
- ACCEPTED: N
- REJECTED: N (synthetic reasons)
- NEEDS REFINEMENT: N

GO.
```

**After the Lambda report, the orchestrator**:
1. For each NEW LESSON marked ACCEPTED:
   - Creates the file `memory/lessons/lesson_<slug>.md` with strict YAML frontmatter (cf. template `memory/lessons/lesson_template.md`)
   - Initial fields: `uses: 0`, `last_hit: NEVER`, `source_episodes: [YYYY-MM-DD_slug_current_episode]`, `status: active`, `importance_history: []`
2. For each UPDATE marked ACCEPTED:
   - Updates the frontmatter of the existing lesson file: `uses += 1`, `last_hit = today`, append current episode to `source_episodes`
3. For each REVISION marked ACCEPTED:
   - Changes `status: active → review` in the frontmatter
   - Appends a comment (## Revision note) in the body with the Lambda justification
4. Updates `memory/INDEX.md`: adds new lessons in their respective domain sections; reflects updates (uses, last_hit) and revisions (status).
5. Updates the current episode frontmatter: fills `lessons_validated_by_lambda` with the effective list of validated slugs (field renamed in v2.2 from `lessons_validated_by_user`).
6. **Trigger attention layer rebuild (v2.3)**: if at least one creation / update (that modifies the body or encoded fields) / revision was applied in steps 1-3, the orchestrator automatically runs:
   ```
   python3 $COMMONTRACE_ROOT/memory/attention/build_index.py
   ```
   Expected output: `Index built: N lessons, model=multi-qa-mpnet-base-dot-v1, dim=768`. If the script fails (sentence-transformers unavailable, etc.), the orchestrator notes it in the final report but does not block the run — the index remains queryable as-is for future runs. Manual rebuild also possible: `python build_index.py --force` after manual editing.
7. Includes in the final user report:
   - List of ACCEPTED proposals applied
   - List of REJECTED proposals with Lambda reason (traceability)
   - List of NEEDS REFINEMENT proposals with fields to correct (the user can decide to rework them manually)
   - Attention layer rebuild status (OK / KO / not triggered if nothing applied)

**Skippable** via `--skip-omega` (skips 9 + 10 + 11 together — attention rebuild is also skipped since it is conditioned on Lambda application).

## Memory

### Base path

Skill memory base: `memory/` (path relative to this `SKILL.md`, i.e. `$COMMONTRACE_ROOT/memory/`).

### Importance rubric (v2.1, 1-5)

Each episode AND each lesson carries a scalar field `importance` (integer 1-5) accompanied by an `importance_rationale` (mandatory 1-sentence concrete string). The rubric:

```
Importance 5 (showstopper): without this lesson, the entire task class fails
                             or causes data loss / security breach.
Importance 4 (critical):    ignoring this lesson → high probability of major
                             rework or a subtle hard-to-detect bug.
Importance 3 (useful):      the lesson avoids a common anti-pattern or
                             methodological trap. Saves significant time.
Importance 2 (minor):       valid lesson but limited impact, applicable to
                             a specific sub-case.
Importance 1 (anecdotal):   interesting observation but not very actionable
                             → prefer documenting as an episode note.
```

**Inspiration**: Park et al. 2023 "Generative Agents: Interactive Simulacra of Human Behavior" (memory stream — they use 1-10, here we choose 1-5 for simpler calibration).

**Usage**:
- **Alpha (Phase 0)** sorts retrieved lessons by `score = importance × tag_match` descending and **ALWAYS considers lessons with `importance >= 4`** even if `tag_match == 0` (safety: a showstopper is likely cross-cutting).
- **Omega (Phase 10)** calibrates the produced episode's importance and proposes the importance of candidate lessons (derived from `source_episodes`, adjustable +/- 1 by Omega when proposing).
- **Omega criterion for proposing a new lesson**: (A) source episode importance ≥ 3 AND generalizable beyond the project, OR (B) importance 4-5 even on a single occurrence. Replaces the old criterion "≥ 2 episodes show it" (too strict for showstoppers).

**Backward compatibility**: for episodes/lessons written before v2.1 without `importance`:
- Alpha treats the absence as `importance = 3` by default (median) and flags "needs calibration" in the "Lessons consulted" block.
- Omega flags "importance absent — needs calibration" in its update proposals.
- No existing script breaks: the fields are additive.

### Future evolutions (not implemented in v2.1)

Documented here for reference — DO NOT implement without explicit user validation and without empirical ground:

- **Temporal decay**: weight `importance` by `exp(-(today - last_hit) / tau)` to surface recently-hit lessons. Risk: forgetting rare but critical lessons. Probable coupling with a separate `recency` factor.
- **Separate recency**: maintain a `recency` field distinct from `importance` (Park et al. use this decomposition). To discuss when we have more empirical data on Alpha retrieval.
- **Auto bump uses → importance**: if a lesson exceeds N hits over M runs, auto-increment its importance (strong empirical utility signal). To discuss: risk of drift toward everything at importance 5.
- **Composite salience**: `salience = α·importance + β·recency + γ·log(uses+1)`. Park et al. style. Requires tuning α/β/γ.
- **Multi-dimensional**: move from scalar to vector (e.g. `importance = (severity, frequency, generalizability)`). More expressive but requires a more sophisticated retrieval UI.

These evolutions are future avenues for the CommonTrace platform. In v2.1, we stick with scalar 1-5 + rationale, period.

### Memory architecture

```
memory/
├── INDEX.md                  # hierarchical index by domain (edited at each Phase 11)
├── episodes/
│   ├── README.md             # episode format + workflow
│   ├── episode_template.md   # empty template
│   └── YYYY-MM-DD_<slug>.md  # one file per /commontrace run (written by Omega Phase 10)
└── lessons/
    ├── README.md             # lesson format + workflow
    ├── lesson_template.md    # empty template
    └── lesson_<slug>.md      # one file per lesson (created by orchestrator Phase 11 after validation)
```

### When each agent intervenes

- **Alpha** (Phase 0, by default): memory retrieval before code. Skippable via `--skip-alpha`.
- **Omega** (Phase 10, by default): synthesis + proposals after B verdict. Skippable via `--skip-omega`.
- **Lambda** (Phase 11, by default): automatic memory backlog validation after Omega proposals. Coupled to `--skip-omega` (skipped together). Memory is never enriched without an ACCEPTED verdict from Lambda — 100% automated workflow, no human dependency.

### Link with CommonTrace Platform

This mechanism (Alpha + Omega + memory base) is an experimentation ground for the CommonTrace fleet-learning platform, whose objective is to evaluate long-term agent learning (lesson quality, implicit contextual retrieval, isomorphic situation transfer).

### Design choices retained (v2 + v2.2)

- **Hierarchical base by domain** (Option B): INDEX.md sectioned by domain (git-safety, cuda-gpu, refactor, testing, subagents, performance, other) to facilitate Alpha pre-filtering.
- **Automatic Lambda validation** (Phase 11, v2.2): memory is never enriched without an ACCEPTED verdict from an independent reviewer of Omega's choices. Lambda audits formal quality, non-duplicate, generalization, importance calibration. 100% automated workflow, usable by an agent without any human in the loop (overrides user validation from v2-v2.1).
- **Global scope with project tag**: memory is shared across projects, the `project:` frontmatter field allows cross-project filtering.
- **Alpha failure is non-blocking**: if retrieval fails, we continue with an explicit signal, we do not block the run.
- **Memory separate from global MEMORY.md**: the `$COMMONTRACE_ROOT/memory/` base is independent of any project-level MEMORY.md. Decoupled evolutions.
- **Omega NEVER writes lessons**: only the episode is written automatically (traceability), lessons go through Phase 11 (Lambda audit + orchestrator application).
- **Lambda is to Omega what B is to A**: independent reviewer who judges against explicit criteria, not the author of the proposals. Reproduces the double-review pattern at the memory backlog level.

### Benchmark note (out of scope v2)

A benchmark script measuring `lesson_quality` / `implicit_retrieval` / `transfer_gap` will be added in a separate step.

## Optional parameters (inline)

The user can specify in the invocation:
- `max_iterations=N` (default 3)
- `tests=path/to/tests` (default: entire project)
- `skip-commit-after-a` (use if conflict with other workflows)
- `no-loop` (1 A+B cycle without loop)
- `--skip-alpha` (skip Phase 0 memory retrieval)
- `--skip-omega` (skip Phases 9 + 10 + 11 — no mini-retro, no Omega, no memory write)
- `--alpha-only` (run Alpha alone, display its report and stop — useful for testing retrieval)

Example: `/commontrace refactor xxx --max_iterations=2 --tests=OGHAM/tests/`
Example: `/commontrace fix bug yyy --skip-omega` (urgency: skip learning)
Example: `/commontrace --alpha-only "port CUDA module xxx"` (just to see what memory says)

**Backward compatibility**: all historical invocations continue to work. Alpha/Omega phases are enabled by default but skippable.

## Best practices

### Brief generation
- **A brief must be self-contained**: the sub-agent does not see the conversation. Include all necessary context (paths, conventions, exclusions, Alpha output if applicable).
- **B brief must be independent**: do not say "validate A's claims". Provide the criteria + the code, B judges against the criteria.
- **Alpha / Omega briefs**: copied verbatim from this SKILL.md, not reformulated.
- **No reused sub-agent**: new sub-agent spawned at each cycle to guarantee independence (sending a message to an existing sub-agent is NOT recommended).

### Parallelization
- A and B are **sequential** (B reviews what A produced). No A↔B parallelization.
- **Multiple `/commontrace` in parallel** OK if files are disjoint (cf. `feedback_parallel_subagents_file_overlap`). Verify explicitly before launching.
- **Alpha runs before** Phase 1 (sequential). Not parallelizable with A.
- **Omega runs after** Phase 9 (sequential). Not parallelizable with B.

### Commit messages
Format for intermediate commits:
```
[TASK]: it N (A produced, B not yet reviewed)

[Short summary of A report]
[Passing tests: N/M]

Co-Authored-By: commontrace-agent <noreply@commontrace.dev>
```

For the final commit (CONFORM):
```
[TASK]: CONFORM after N iteration(s)

[Final summary: semantics, perf, tests]

Verdict B reviewer CONFORM.
[File links / results]

Co-Authored-By: commontrace-agent <noreply@commontrace.dev>
```

### Anti-patterns of the skill itself
- Do not skip Phase 4 (immediate commit) — risk of loss
- Do not relaunch the SAME sub-agent via message (loses B↔A independence)
- Do not iterate > 3 times without explicit user arbitration
- Do not launch multiple `/commontrace` on the same files in parallel
- Do not let Omega write lessons without Lambda validation (Phase 11 mandatory if Omega runs)
- Do not block the run on Alpha failure (non-blocking by design)

## Reference user memories

- `feedback_subagent_double_review` — A+B implementer + independent reviewer pattern
- `feedback_no_subagents_for_architectural_code` — exception: OK with double-review (this skill)
- `feedback_parallel_subagents_file_overlap` — serialize on same files
- `feedback_show_changes_before_editing` — A must show its plan in its report
- `feedback_transparency_when_deviating` — A must signal if deviating from spec
- `commontrace-platform` — CommonTrace fleet-learning platform, this mechanism is its reference implementation

## Invocation examples

```
/commontrace refactor InferenceEngine.step() to accept step_runtime_kwargs in addition to step_default_kwargs (init). Criteria: 33 existing inference tests pass, no alias @property, default = V0.3 behavior unchanged.
```

```
/commontrace fix multi-instrument Pressure parity bug in cuda_v3 (diff 0.16 currently). Hypothesized cause: L2-supersede filter ordering or Exhaustion coupling. Criteria: diff < 1e-5 on 10 inst × H1 × 1 month, 533 tests pass, do not touch latents/force_relative.py (B0b territory).
```

```
/commontrace CUDA port of liquidite_gpu.py module to Triton custom kernel. Criteria: bit-exact CPU/GPU parity < 1e-4, speedup ≥ ×3 on 3m smoke, existing 526 tests pass. --max_iterations=2
```

```
/commontrace --alpha-only "refactor module XYZ to vectorize update()"   # just see what memory says
```

```
/commontrace fix production urgency --skip-omega   # urgency: no time for mini-retro + learning
```
