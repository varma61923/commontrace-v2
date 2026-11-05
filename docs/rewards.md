# Graded and multi-objective outcomes

The holdout experiment records one binary outcome per occasion, but real
outcomes are graded and come several at a time:

- a robot finishes 70% of a pick sequence using 30 Wh;
- an LLM answer scores 0.8 with an evaluator and costs 2,400 tokens;
- a coding agent's patch passes 9 of 12 tests.

`unimem.rewards` brings these outcomes into the same evidence chain without
changing any estimator.

```sh
unimem reward configure '{"terms": {"progress": {"weight": 3},
                                    "energy_wh": {"low": 0, "high": 50, "better": "lower"}}}' --dest ./store
unimem reward record --occasion-id pick-1042 --metrics '{"progress": 0.7, "energy_wh": 30}' --dest ./store
unimem reward record --occasion-id answer-77 --reward 0.8 --dest ./store
unimem reward verify --dest ./store
unimem reward effects --dest ./store
```

The same outcome can be sent through any surface:

| Surface | Call |
|---|---|
| HTTP gateway | `POST /v1/outcome` with `{"occasion_id", "reward"}` or `{"occasion_id", "metrics"}` |
| Python | `CausalMemory.record_outcome(id, reward=0.8)`; `MemoryClient.outcome(id, reward=0.8)` |
| MCP agents | the `record_reward` tool, with the `occasion_id` used for `retrieve` |
| Frameworks | `unimem.integrations.langgraph.record_outcome(..., reward=)`; `pydantic_ai.record_outcome(..., reward=)` |
| Robots | `RobotBridge.finish(id, reward=0.7)` or `finish(id, metrics={...})`; the durable `OutcomeQueue` keeps graded outcomes while offline; ROS `/task/end` envelopes accept `reward` or `metrics` |

## Randomized rounding

A reward `r` in `[0, 1]` is recorded as the canonical binary outcome
`succeeded = U < r`, where `U` is the first 64 bits of
`SHA-256("unimem/reward-rounding/v1", salt, occasion)` divided by 2^64.

- **Reproducible.** Any auditor with the experiment salt can recompute `U`.
- **Independent of the arms.** Assignment hashes use a different domain, so
  `U` does not depend on which memories were shown.
- **Fixed in advance.** `U` is set before the reward is known.

So `P(succeeded | arm) = E[r | arm]`. Every downstream estimator is then an
unbiased estimator of the effect on expected reward, with no change: Hajek and
AIPW effects, anytime confidence sequences, e-processes, harm withdrawal and
graduation, the causal gate, the effect field and valuation.

The cost is variance. A rounded reward is a Bernoulli draw with the same mean,
never noisier than an outcome that was binary to begin with.
`unimem reward effects` adds a lower-variance estimate (Hajek over the raw
rewards, on occasions that recorded one) of the same estimand.

`MemoryClient` also passes the unrounded reward to the reflect policy and the
exploration learner, which already learn from fractional outcomes.

## Utility

A utility turns named measurements into one reward:

- It is a positive-weighted mean of terms.
- Each term is scaled to `[0, 1]` between declared `low` and `high` bounds.
- Each term declares a direction: `higher` (default) or `lower` is better.
- A missing term is an error; it is never imputed.

Each reward stores the utility's digest. Configure the utility before outcomes
arrive and treat it as a preregistered value function. `verify` reports when
more than one utility scored the running experiment.

## Integrity

- A reward is recorded at most once per occasion. A different reward is a
  conflict (HTTP 409), as for binary outcomes.
- A reward is never attached to an occasion that already has a binary outcome.
- `verify` recomputes every draw under the current salt. It checks each stored
  bit against its reward and against the outcomes log, so binary outcomes
  cannot be quietly chosen instead of derived.

## Limits

- The salt is the secret behind the rounding draws, as it is behind
  assignment. A reporter who knows the salt can choose a reward that rounds the
  way it wants, which is no stronger than reporting a false binary outcome.
- A utility is a modelling decision. UniMem measures the effect on the utility
  you declared, not on what you meant.
- Remote `MemoryClient` instances and the generated SDKs keep the binary
  `/v1/memory/outcome` contract. Send graded outcomes to `/v1/outcome`.
