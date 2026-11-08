# Measured learning API

`unimem.learning.v1` is shared by the local journal, HTTP gateway, Hub, MCP,
and Python, TypeScript, Go, Rust and JVM clients. It adapts memory selection from
observed task outcomes. It does not train model weights or authorize robot actions.
Existing installations remain unchanged until an owner configures an experiment.
Before starting upgraded Hub workers, apply migration `ff69cbd3e507` with the
separate migration owner using the [deployment procedure](production-readiness.md).
Historical holdout rows without recorded assignment rates remain explicitly
unclassified; the new evaluator never invents their missing propensities.

## Local example

```python
from unimem.client import MemoryClient
from unimem.memory_record import MemoryRecord

memory = MemoryClient(root="./mem")
memory.put_record(
    MemoryRecord(
        id="check-calibration",
        kind="fact",
        version=1,
        payload={"text": "Check calibration before precision work."},
    )
)
experiment = memory.learning.configure("observe")
assignment = memory.learning.assign("task-001", ["check-calibration"])
# Serialize assignment["memories"] into the agent's actual task context first.
# Record only the IDs actually delivered, including [] for a control occasion.
memory.learning.delivery("task-001", assignment["inject"], experiment_id=assignment["experiment_id"])
# Report an observed result from the application or evaluator.
memory.learning.outcome("task-001", reward=0.8, experiment_id=assignment["experiment_id"])
report = memory.learning.run()
```

Use `MemoryClient(url=..., token=...)` for the gateway; async `HubClient.learning`
uses the same operations. Restricted agent credentials can assign, acknowledge
and report within their principal scope; only the local owner or Hub administrator
can configure, run or roll back a selection policy. Configuring a different mode
starts a new experiment. Pin the returned `experiment_id` on delayed receipts and
outcomes so reconfiguration cannot attach them to another experiment.

Local experiments are configured per exact scope. An owner can prepare a robot's
scope with `MemoryClient(root="./mem", context={"agent": "robot-1"}).learning.configure("observe")`;
the runtime then connects with `MemoryClient(root="./mem", agent_id="robot-1")`.
Hub configuration is tenant-wide, while occasion identities are principal-bound
and status results are filtered to the caller's authorized sources.

## Protocol

| Operation | HTTP | Request |
| --- | --- | --- |
| capabilities | GET `/v1/learning/capabilities` | none |
| configure | POST `/v1/learning/configure` | `mode`, optional `utility`, `holdout_rate`, `min_arm` |
| assign | POST `/v1/learning/assign` | `occasion_id`, `memory_ids`, optional `task_context`, `experiment_id` |
| delivery | POST `/v1/learning/delivery` | `occasion_id`, actually delivered `memory_ids`, optional `experiment_id` |
| outcome | POST `/v1/learning/outcome` | `occasion_id`, exactly one of `succeeded`, `reward`, `metrics`; optional `experiment_id` |
| run | POST `/v1/learning/run` | optional `experiment_id`, `limit` |
| status | GET `/v1/learning/status` | optional `experiment_id` query |
| rollback | POST `/v1/learning/rollback` | `reason`, optional `decision_revision`, `experiment_id` |

MCP names are `learning_` followed by the operation. Capability negotiation rejects
unsupported schema versions instead of quietly falling back to legacy outcome APIs.
The gateway accepts owner `context` labels; authenticated agent scopes override
caller labels. Hub authority comes from its authenticated tenant and principal.
`task_context` is a pre-task experimental stratum, never an authorization scope.
Transport access uses normal scoped credentials or Hub SSO. Existing attenuated
transport capabilities do not grant the new learning routes; those routes remain
denied for delegated requests.

Modes are `off` (no withholding or adaptive changes), `observe` (randomized evidence
and reports), and `adaptive` (evidence can update selection). Protected directives
are never randomized. Default withholding is 0.5; accepted rates are 0.05–0.95.
Assignments accept 1–100 unique canonical source IDs and resolve current authorized
content themselves. Delivery accepts 0–100 IDs, all within the assigned injection.
Resolve candidate IDs inside the application's retrieval stage, before exposing
source bodies to the task. Render only the assignment's selected `memories`.
An earlier full-content lookup by the agent would contaminate a withheld arm;
use a single assignment protocol for each occasion.

`reward` is finite in [0, 1]. `metrics` requires a utility frozen at configuration:

```json
{"mode":"observe","utility":{"terms":{"completion":{"weight":1,"low":0,"high":1,"better":"higher"},"energy_wh":{"weight":0.2,"low":0,"high":50,"better":"lower"}}}}
```

Every required metric must be supplied. A deterministic, domain-separated reward
draw feeds the existing binary statistical engine; the original measurement and
utility digest remain auditable. Identical retries succeed; conflicting receipts
or outcomes return 409. Unknown sources/experiments return 404; malformed requests
return 400, and unauthorized administration returns 403.
Hosted numeric-worker saturation returns retryable 503; accepted pending work
remains durable while the worker backs off.

## Evidence and recovery

Assignments freeze source revisions, randomization rates, utility and context.
Incomplete delivery or unresolved outcomes block decisions for the affected
source/context cohort. Missing observations are never silently dropped. The
canonical integrity audit, sequential analysis and family multiplicity correction
gate adaptation. Proven helpful memories retain a 5% monitoring holdout; harmful
memories are disabled for their exact revision/context. Insufficient evidence
leaves the existing selection unchanged. Changed or revoked sources cannot be
readmitted by historical evidence or rollback.

Accepted measurements and pending numeric work are persisted atomically. The
local leased job queue/daemon and Hub SQL workers resume work without model
credentials. A pass is bounded to 10,000 assignments by default; exceeding that
budget refuses adaptation and is visible in the report. `status` exposes the
pending checkpoint, decision revision and latest report. Rollback appends a new
policy revision and retains evidence. Learning receipts retain source IDs and
revisions rather than copied source bodies.

## Reproducible verification

```bash
python -m benchmarks.learning_stream --check --out /tmp/learning-results.json
```

This chronological simulation compares absent, frozen and adaptive memory on
separate training/evaluation occasions, with binary agent results, graded LLM
scores and simulated robot telemetry. Context shifts reverse the effects of the
same source. The artifact records the seed, evidence digest, decisions and primary
research links. It is a regression test, not a model leaderboard or physical robot
validation. Real deployments must measure their own task outcomes.

The benchmark design draws on [CoALA](https://arxiv.org/abs/2309.02427),
[MemGPT](https://arxiv.org/abs/2310.08560), [A-MEM](https://arxiv.org/abs/2502.12110),
[Mem0](https://arxiv.org/abs/2504.19413), and
[Continual World](https://arxiv.org/abs/2105.10919). These references motivate memory
interfaces and contextual evaluation; they do not establish comparative superiority.
