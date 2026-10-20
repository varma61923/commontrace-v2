# Human corrections and the value of asking

A warehouse robot's teleoperator takes over a failed grasp. The ledger preserves
the admitted pre-intervention window and corrective segment, the enrolled
operator and skill tier, reported time, and an advantage label derived from the
actual canonical task outcome. A support agent can use the same workflow for a
human correction to an answer.

This addresses the problem of treating all interventions as useful training
examples. It is motivated by RECAP in
[π*0.6: a VLA That Learns From Experience](https://arxiv.org/abs/2511.14759).
The implementation does not run that model or reproduce its training pipeline.

## Correction evidence

Enroll a scoped human identity before collecting correction-quality data:

```sh
commontrace fleet operators '{"action":"register","operator":"operator-42","principal":"human-42","skill_tier":"trained","context":["project:depot"]}' --dest ./store
```

Skill tiers are `novice`, `trained` and `expert`. Enrollment authenticates the
operator's assertion about a human; it does not establish physical identity or
prove a takeover occurred. Each enrollment fixes its ownership scopes and a
`quality_horizon` of 40–4096 interventions. Use separate scoped enrollments for
different ownership boundaries.

`fleet interventions` accepts `action: "record"` with `occasion`, `unit`,
`operator`, admitted canonical `pre_window` and `corrective_segment` ID lists,
a frozen `baseline` prediction in [0,1], `operator_seconds`, `episode_seconds`,
`started_at`, `ended_at`, and `context`. For a randomized memory unit, the canonical assignment must already
exist and its outcome must be unresolved. Use `unit: null` for an ordinary or
safety intervention without a randomized unit; it joins the ordinary canonical
occasion outcome and does not create a treatment assignment. The outcome-file lock prevents an
outcome from racing the evidence commitment. Each segment is bounded to 32 KiB;
the complete managed record retains source scopes and erasure links.

After the ordinary outcome engine resolves the task, `fleet operators` with
`action: "report"`, `operator` and `context` computes quality from those canonical
outcomes. A caller cannot submit a success label to this report. The advantage
is actual success minus the pre-outcome baseline prediction. This is a
prediction residual, not a causal comparison between operators: task difficulty,
baseline calibration and assignment to operators still matter.

Quality uses fixed consecutive blocks, with the horizon frozen at enrollment.
Unresolved or erased members cannot be dropped. Conservative bounded-residual
intervals spend alpha over at most 64 enrolled operators and successive fixed
blocks. An upper endpoint below −0.1 quarantines the corrector; a nonnegative
lower endpoint makes a complete block eligible. Other cases remain in review.
A bad completed block remains a hold unless reviewed; later favorable data does
not silently erase it. The intervals require the usual bounded-observation
sampling assumptions, and do not remove confounding.

The G2 RL export boundary checks correction sources and their lineage before
creating training bytes and again before each streamed shard. It blocks the
complete cohort rather than dropping unfavorable examples. Source erasure
invalidates the evidence and holds eligibility instead of restoring trust.

An appeal uses `fleet operators` with `action: "appeal"`, `operator`, `reason`
and `context`; it changes no training permission. A distinct authorized reviewer
can submit `action: "review"`, the current `report_digest`, `decision` of
`approve` or `reject`, and a reason. A corrector cannot approve their own data.
The review binds the exact current report; changed or erased evidence invalidates
it. Other source, privacy and safety checks still apply.

`fleet interventions` without an action reports active admitted windows,
interventions per hour, and reported operator minutes. The Python equivalent is
`recovery.intervention_ledger`. Durations are reported measurements, not
hardware-attested execution time.

## Asking trials and budgets

Asking experiments are off by default. Only an undelegated operator can enroll a
context/question-type pair and explicitly classify it `low-risk`. Safety and
unknown-risk contexts never randomize. Protected hazard sources are checked
again at decision time. The requesting planner must be enrolled; delegated
planners additionally need a G6 capability for namespace `ask`, operation
`write`, class `question`, their actual purpose and the context's labels.

Start the canonical experiment using the existing experiment setup, then enroll:

```sh
commontrace fleet ask-policy '{"action":"configure","context":"support","question_type":"answer-format","risk":"low-risk","enabled":true,"budget_seconds":1200,"cost_seconds":30,"trial_size":40}' --dest ./store
commontrace fleet ask-policy '{"action":"decide","context":"support","question_type":"answer-format","occasion":"ticket-42"}' --dest ./store
```

The allocation schedule takes effect after its standard five-second publication
delay. Before that, the planner returns operator review without randomizing.
The CLI and MCP `should_ask` return the same reserved decision on retry. Context
and risk enrollment are trusted coordinator assertions; applications must bind
the actual task context rather than let an agent relabel a safety task.

Actual `ask:<context>:<question-type>` assignments come from the existing typed
trial engine and canonical holdout log. The returned propensity is read from
that log. The caller must follow the assigned action; a signed ledger does not
prove delivery or execution.

Every policy shares one immutable fleet-time budget. It reserves the maximum
configured cost for either arm **before** drawing. A budget failure cannot turn
an assigned ask into a proceed sample. Pending reservations survive crashes and
retries. Content erasure never refunds this accounting. Concurrent planners
cannot reserve the same remaining seconds. Re-enabling a disabled policy
preserves its original cohort, design and reservations; it cannot reset a trial.

After the normal canonical outcome is recorded, `fleet ask-policy` with
`action: "cost"` records `operator_seconds` for that occasion. Both arms need a
time report. Human time exceeding a reservation is explicitly reported as an
overrun; a software reservation does not bound a person's physical behavior.
`action: "report"` requires the complete registered cohort, actual costs and
canonical integrity checks. It reports IPW success lift, a conservative interval,
reported minutes, and an estimate of minutes saved relative to always asking.
Missing, erased or extra episodes cannot silently become a complete report.

The planner's prior follows the belief/utility idea in
[Structured Uncertainty guided Clarification for LLM Agents](https://arxiv.org/abs/2511.08798).
Optional finite `beliefs` and action-by-state `utilities` compute EVPI as expected
best utility with perfect information minus the best current expected utility.
This upper bound is a model assumption, not the measured value of a particular
question. Without a belief model, an operator can enroll a bounded scalar prior.

Optional `calibration` lists 3–8 other completed context/question-type pairs.
Enrollment resolves their actual canonical reports in the same ownership scopes
and fits Gaussian random-effects empirical-Bayes bias and variance parameters.
It freezes their digests before the new trial. Managed evidence links propagate
upstream erasure into the learned prior and dependent policy. `planner_value`
uses this shrinkage model; `effect_ipw` remains the separate unbiased estimator
under its stated sampling assumptions. The Bayesian planner score is not a
confidence bound or causal execution proof.

`action: "plan"` accepts tasks with known risk, alternative modes, seconds and
bounded values, plus `budget_seconds`. It returns `ask`, `dont-ask`, or
`which-mode` using a deterministic value-per-second heuristic. Reserved seconds
never exceed the supplied budget. Safety and unknown-risk tasks return safe
escalation rather than spending an experimental budget to proceed.

## ROS 2 service

Build the real service definition when ROS 2 is installed:

```sh
cd ros2
colcon build --packages-select commontrace_interfaces
source install/setup.bash
```

`robotics.bridge.create_node` accepts an explicitly configured `on_ask` callback
and registers `commontrace_interfaces/srv/ShouldAsk`. The request carries an
occasion and question type; the bridge supplies its own embodiment/site/skill
context. The default service name is
`/commontrace/robot_<sha256(robot-id)[:16]>/should_ask`, avoiding cross-robot name
collisions. Set `topics["ask_service"]` for an explicit deployment name.

The callback can bind `human_help.should_ask` to a locally authorized store.
Use the deployment's authenticated DDS/SROS 2 graph for service access; the
callback does not establish a remote human's identity. It returns bounded JSON
decisions and fails to safe escalation on errors. It sends no motor command.
Native generated-interface tests run when ROS 2 and the generated package are
available; ordinary CI also exercises service dispatch through an injected node.
