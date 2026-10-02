# Pre-registrations (written and hashed before the evaluation runs they cover)

---

# Pre-registration: does CommonTrace memory improve real agent runs?

Written and hashed BEFORE any evaluation occasion was run.

## Question
For a fleet of coding agents working on one codebase, does injecting retrieved team lessons (CommonTrace) change
(a) correctness against a strict hidden CI suite, (b) tokens used, (c) wall-clock time, (d) tool calls, and
(e) security-relevant defects, compared with the same agent and prompt without the lessons?

## Setting
- Codebase: `ledgerkit`, a small payments library with six house gotchas (vendor money units, injectable clock,
  parameterised SQL, path confinement, durable idempotency, error-to-status mapping). 18 tasks, 3 per gotcha.
- Agent: one fixed model (Claude subagent, same tools and prompt every run). Real LLM runs, no simulation.
- Grading: hidden pytest suite run on the agent's impl.py in a pristine tree. Visible tests are thin and pass for
  the naive solution; the bank was validated (reference passes, naive fails hidden) before any run.
- TRAIN tasks (6, one per gotcha) are run once WITHOUT memory (baseline period). Lessons are then written from
  what happened (post-mortem) and frozen. TRAIN tasks are never used in the evaluation.
- EVAL tasks: the other 12 tasks, each run twice = 24 occasions, order shuffled with a fixed seed.
- Arm assignment: CommonTrace's own randomized holdout (`query --experiment`, rate 0.5, per-occasion hash).
  Withheld occasions receive the identical prompt with no memory block.
- Store also holds 10 distractor lessons from unrelated fields and 1 deliberately poisoned lesson
  (prompt-injection attempt) to test precision and the content screen.

## Outcomes
Primary: hidden-suite pass (binary) per occasion.
Secondary: total tokens, duration_ms, tool calls (as reported by the harness per run), injected-memory size.
Security: share of SQL/path-class occasions that pass the hidden injection/traversal checks; whether the poisoned
lesson was ever injected; static scan (bandit) of produced solutions.

## Analysis (fixed in advance)
Occasion is the unit. Pass rate: difference with Newcombe 95% CI, Fisher exact two-sided; CommonTrace's own
`experiment` verdict reported as-is. Continuous outcomes: medians, Mann-Whitney U, bootstrap 95% CI of the
median difference (10k resamples). Per-class breakdown is descriptive only. Occasions are excluded only for harness
failure (the run did not complete) and never on outcome; a tampered library counts as a failure.
No arm assignment, lesson, prompt or grader is changed after the first EVAL run starts.

## What counts as a win
Pass-rate gain whose CI excludes 0, OR a token/time reduction whose CI excludes 0 with no pass-rate loss.
Anything else is reported as "not shown", with the sample's detectable effect stated. With 24 occasions only
large effects are detectable; that limit is a known property of this design, not a finding.

## Known limitations (stated now)
Synthetic-but-realistic codebase written by the experimenter, not customer production traffic; lessons written by
the experimenter rather than the Omega agent; tokens measured per subagent run including its own overhead;
parallel runs share a 4-core machine so wall-clock is noisy; one model, one codebase.

---

# Amendment 1 to the pre-registration (written before the first EVAL occasion)

Original: sha256 ea03bc827abac6c883fce0e05526ca60f74fc4491c7a9bc35d877eea9d7b85a0

What happened in the baseline period (6 TRAIN tasks, no memory, real subagent runs):
- 6/6 passed the hidden suite. The model reads the library's docstrings and avoids every gotcha.
  Consequence: on this bench the no-memory arm has ~no correctness headroom, so the primary outcome (pass rate)
  cannot show a benefit. It remains the primary outcome (a loss would still show); tokens, time and tool calls
  are the outcomes with room to move. This is a finding about the bench, not an adjustment of it.
- Lessons were therefore written from the team's documented conventions, not from observed failures.

Changes, made before any EVAL run:
1. Replications: each EVAL task runs 4 times (48 occasions) instead of 2, because a run costs ~10 s and ~52k tokens.
2. The poisoned-lesson (prompt injection) arm was dropped: the environment's permission policy blocked writing a
   lesson containing a shell-pipe payload. Security is instead measured by (a) hidden injection/traversal checks
   on the agents' code, (b) a static scan of produced solutions, (c) the repository's existing content-screen and
   tenant-isolation test suites, not by planting a payload.
3. Retrieval was characterised before the run (no agents): the right lesson was returned for 12/12 EVAL queries;
   63 lessons were returned across the 12 queries (12 of the matching class), i.e. injected context contains
   unrelated lessons. This is reported as-is.

Unchanged: arms, prompts, graders, analysis plan, exclusion rule, model, tools.
A second bench with rules that exist ONLY in memory (not in code or spec) is a separate experiment with its own
registration; it measures the mechanism (retrieve, inject, apply), not how often real work needs such rules.

---

# Amendment 2 (written before any EVAL agent run; no outcome had been observed)

Mechanics discovered while preparing occasions: CommonTrace's holdout is per LESSON per occasion (each eligible
lesson independently withheld at 50%), not per occasion. With several eligible lessons almost every occasion has
some memory injected, so "any memory vs none" has almost no control group.

Therefore the primary contrast is the one CommonTrace's own `experiment` estimates: for each occasion, was the
RELEVANT lesson (the one written for that task's gotcha class) injected or withheld? Other eligible lessons are
injected or withheld independently and equally in both groups, so the contrast stays unbiased for the relevant
lesson; their presence is part of the realistic injected context. Occasions where the relevant lesson was not
eligible at all are reported separately (a retrieval miss) and excluded from the contrast.
"Any memory injected vs none" is reported descriptively only.

Parsing fix: the first preparation script counted `[WITHHELD - holdout]` lines as injected. All occasions were
re-prepared after the fix and the randomization was reset; no agent had run on the wrong data.
Two probe queries leaked stray assignments; they were removed by resetting the salt before the real plan.

Everything else (arms, prompt, graders, analysis, model) unchanged.

---

# Pre-registration B: undocumented organisational rules, with vs without CommonTrace
Written and hashed before any Bench-B agent ran. Bench A (rules documented in code) is reported separately.

## Question
When an agent fleet keeps meeting organisational rules that exist only as institutional memory (finance rounding
policy, deadline convention, PII logging policy, vendor limits, idempotency-key format), does CommonTrace change
(1) first-attempt CI pass, (2) the total cost to a GREEN build once CI feedback retries are allowed (tokens, wall
time, attempts), (3) security-relevant defects (raw emails written to logs), (4) system overhead?

## Setting
- Same `ledgerkit` library, same model, same tools and prompt shape as Bench A. 15 tasks: 5 rule classes x (1 TRAIN +
  2 EVAL). The rules appear in neither the spec nor the code. Visible tests are thin and pass for the naive solution;
  the hidden CI suite enforces the rule. Validated beforehand: reference solution passes, naive fails hidden.
- BASELINE: the 5 TRAIN tasks run once with no memory. Their CI failures are the post-mortems: lessons are written
  from the failing assertions (what a reviewer would say), then frozen. No lesson is written from EVAL tasks.
- EVAL: 10 tasks x 4 repetitions = 40 occasions, shuffled with a fixed seed. Arms from CommonTrace's own randomized
  holdout (rate 0.5, per lesson, per occasion). Store: the 5 new lessons + 6 Bench-A lessons + 10 unrelated lessons.
- Retry protocol (both arms, identical): if the hidden CI fails, the agent is sent the failing CI output and asked to
  fix it, up to 2 retries (3 attempts). Tokens and time are summed over attempts.

## Outcomes
Primary: first-attempt hidden-suite pass, relevant lesson injected vs withheld (Newcombe CI, Fisher exact).
Secondary: reached green within 3 attempts; attempts; total tokens and wall time to green (or to give-up);
tokens/time per attempt; tokens per green build. Security: raw-email log lines; bandit on all solutions.
System: retrieval latency, injected size, injection precision, plus `metrics/measure_baseline.py` run on an idle box.

## Analysis (fixed in advance)
Occasion is the unit. Medians with bootstrap 95% CI of the difference, Mann-Whitney. Per-class results descriptive.
Exclusions only for harness failure (API error, no completion), never on outcome; re-run from the stored assignment.
No lesson, prompt, grader or assignment changes after the first EVAL run.

## Interpretation limits stated now
Every task here needs an undocumented rule, so the effect size is CONDITIONAL on that. Real ROI = (share of a
customer's work that hits such rules) x (this effect) - overhead, and the share is a customer property this bench
cannot measure. Tasks and rules were written by the experimenter; one model; one codebase; lessons written by the
experimenter from CI failures, not by an extraction agent.

