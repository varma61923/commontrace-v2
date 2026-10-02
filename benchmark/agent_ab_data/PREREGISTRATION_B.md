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
