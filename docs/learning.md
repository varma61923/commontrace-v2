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

## Supported interfaces

All supported interfaces negotiate `unimem.learning.v1` before a learning
operation. Unsupported operations or versions fail explicitly. The limits and
immutable evidence semantics are also returned by `learning/capabilities`.

| Interface | Release / runtime boundary | Verified behavior |
| --- | --- | --- |
| Embedded Python | UniMem 1.0.0; Python 3.10+ | All eight operations on the canonical SQLite journal; exact scoped identity, transactional evidence and local worker recovery |
| Local HTTP | Same installed UniMem version; versioned `/v1/learning` routes | All eight operations; owner administration and registered-agent scope binding; same experiment and receipts as embedded Python |
| Local MCP | Python `mcp` 2.x extra | All eight operations for the local operator; restricted toolsets omit administration; scoped replay preserves authenticated authority |
| Hosted HTTP / MCP | Hub from the same release; PostgreSQL 16; Hub pins `mcp` 2.3.0 | All eight operations; native tenant/principal-scoped SQL, audit transactions and fenced workers; additive migration `ff69cbd3e507` required |
| Python hosted SDK | UniMem 1.0.0 `HubClient.learning` | Async negotiation, pinned experiments, copied request inputs and explicit retry boundaries |
| TypeScript SDK | `sdk/typescript` 0.1.0 | All eight HTTP operations; build and client contracts checked in CI |
| Go SDK | `sdk/go`, source revision matched to this release | All eight HTTP operations; native race-enabled CI suite |
| Rust SDK | `sdk/rust` 0.1.0 | All eight HTTP operations; native build and client contracts checked in CI |
| Java / Kotlin SDK | `memory-client` 0.1.0; Java 17+ | All eight HTTP operations; Kotlin uses the same Java facade; native CI builds both languages |
| Robot bridge | UniMem 1.0.0 planner-level adapter | Assignment, delivery, outcome and status with experiment pinning and durable queued outcomes; simulation/protocol support, hardware acceptance remains deployment-specific |

The learning `task_context` is a normalized experimental label, not a replacement
for a robot's coordinate frame/calibration or a conversation's provenance and
temporal validity. Preserve those domain fields in canonical source records and
their versioned payloads; learning binds their exact revisions. Arbitrary model
weight training, automatic actuation and vendor-specific device drivers are not
part of this contract. Existing framework wrappers that use legacy outcome APIs
do not automatically become measured-learning experiments: integrations must
explicitly assign candidate IDs before exposing source bodies and acknowledge
their actual delivery.

## Public conformance and release measurements

The packaged conformance vectors include the measured-learning layer. It covers
configuration, protected-channel precedence over adaptive policy, contextual
assignment, binary/graded/utility outcomes and invalid-request refusals. Both
copies of `conformance_vectors.json` are checked against the reference. Run an
independent implementation as a JSON-lines program with:

```bash
unimem conformance vectors
unimem conformance exec 'python -m unimem.conformance' --only learning --json
```

Each learning vector contains a `request` and an exact expected `response`.
The reference request uses `op: learning`, an `operation`, a `payload`, and,
where needed, a frozen `config`, canonical `sources`, and selection `policy`.
Those sources represent backend-resolved values in an isolated protocol test;
they are never accepted as authoritative source bodies by the production API.
Older implementations may explicitly select their supported layers with
`--only`; empty vector sets and programs that exit unsuccessfully cannot pass.

For a real end-to-end check, use the built-in disposable-store harness:

```bash
# Core profiles need no model provider or external service.
unimem conformance learning --profiles embedded,http,robot --samples 12 --json
# Include the installed MCP extra; a missing requested dependency fails the check.
unimem conformance learning --profiles all --samples 12 --json
```

The harness uses one registered agent and one experiment across embedded Python,
a real loopback HTTP server, the robot HTTP bridge and optional MCP. It checks
identical assignments, protected facts, explicit delivery, three outcome forms,
store reopening, late outcomes after reconfiguration, scope denial, conflicting
retries, source correction, erasure and durable robot queue replay with the
original utility. It creates and removes its own store and does not modify any
configured store or remote deployment.

JSON includes runtime/platform versions, exact corpus and request counts,
per-adapter/operation p50/p95/p99 latency, initial/final store bytes, disk growth,
process-lifetime peak RSS where available, expected refusals and every failed
check. Latency includes SDK negotiation and rejected requests. The workload is
single-caller SQLite plus loopback HTTP; these measurements do not establish
managed-service load capacity or physical robot timing. CI preserves the report
as `learning-conformance-<commit>` alongside its full regression gates. Hosted
SQL admission, tenant isolation and lease/crash behavior are checked separately
by `hub/tests/test_learning.py` and the enforced-role CI stage.

Robot queue identities include endpoint, experiment, normalized scope and
occasion. Acknowledging one identity cannot settle another experiment's outcome;
ambiguous unqualified acknowledgements are refused. Existing journal entries
without queue IDs remain readable, and their acknowledgements apply only to
the preceding entry. Retryable HTTP errors preserve their status through
`GatewayUnavailable` and retain the queued measurement for credential renewal,
late assignment arrival or service recovery.

Each enqueue also receives a generation receipt. A delayed reply from before
journal rotation cannot acknowledge a newly queued measurement with the same
logical identity. Replay commits completed replies in batches of at most 32;
if a later request fails, the completed prefix remains settled. A process crash
before a batch commits can resend that batch, using the server's immutable,
idempotent outcome contract. Existing journals retain their original receipts.

The robot bridge holds at most 128 unfinished tasks, including requests in
flight. Capacity exhaustion and overlapping operations on the same occasion
raise `ValueError`; callers can retry after the active operation finishes.
Different occasions can make network calls concurrently. Legacy tasks and
learning tasks cannot share an active occasion. Successful completion or a
durably queued offline outcome releases local task state and its action context;
a failed request without a durable queue retains the task for retry. Learning
assignment retries preserve acknowledged delivery. After restart, pass the
original `experiment_id` explicitly when recording delivery or outcome.

## Reproducible learning evaluation

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
