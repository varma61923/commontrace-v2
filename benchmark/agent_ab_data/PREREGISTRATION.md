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
