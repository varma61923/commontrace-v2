# Physical-AI integration

CommonTrace supplies planner context and evidence. Robot interlocks, fresh
sensors, emergency stops and human escalation remain in the robot control stack.

A minimal Python integration needs a running gateway and its bearer token:

```python
from commontrace.robotics.bridge import HTTPGateway, RobotBridge
from commontrace.robotics.edge import OutcomeQueue

robot = RobotBridge(
    HTTPGateway("http://127.0.0.1:8787", token),
    embodiment="parallel-jaw", site="depot", skill="grasp", robot="robot7",
    outcome_queue=OutcomeQueue("/var/lib/commontrace-edge"),
)
context = robot.start("episode-42", "Recover the dropped object")
# Planner consumes context["deliver"], then executes through its normal controls.
robot.finish("episode-42", succeeded=True)
```

Context is the `embodiment/site/skill` label, normalized like any gateway occasion
context (at most 64 characters, so e.g. `franka-panda-parallel-jaw/depot-7/pick`
fits; labels must not contain `/`). `/v1/recall` and `/v1/outcome` retain the
existing gateway contract and authentication. If an outcome request fails, the
signed durable queue survives restart and retries with the same occasion id.
Conflicting final outcomes are refused. Calling applications should flush
`OutcomeQueue.flush(transport.post)` after reconnecting. Once every queued outcome
is acknowledged the queue restarts its journal with a signed link to the retired
chain, so a long-lived robot never fills the bounded journal.

For ROS 2, install rclpy/std_msgs through your ROS distribution and the
`robotics` extra. `create_node` subscribes to configurable task-start, task-end
and success topics, and publishes JSON memory envelopes on the planner topic.
Failure and scene topics use explicitly supplied scoped handlers for recovery
and spatial memory. This node never publishes motor commands. A malformed or
undeliverable message is logged through the node's logger and dropped; it never
raises into the executor (which would stop the node). CI injects a mock
rclpy-compatible node and exercises recall, outcome and scene callbacks.

`frontier robot-ingest` accepts `format`, `path`, `environment` and `robot`.
LeRobot JSONL metadata needs no tensor stack; MCAP requires `commontrace[mcap]`.
MCAP reads only bounded JSON episode channels. Episode summaries and sensor
readings become governed traces; SHA256 media references preserve addresses.
Raw image/audio/video decoding and conversion to a training dataset stay at the
edge. Native LeRobot Parquet/video conversion is a deployment adapter, not
performed by this metadata ingest.

`edge.publish_update` selects governed protected hazards and proven facts,
signs the manifest, anchors it and includes a Merkle consistency proof.
`EdgeCache` pins the signer and its last witnessed head; unsigned, older and
forked updates fail closed. Hazard context survives partial refreshes and the
cache's periodic compaction, which keeps the latest verified bundle plus every
hazard a verified bundle delivered. An
explicit reviewed reset is required for decommissioned hazards. Offline cache
content has the age of its last signed update: enforce freshness/sensor checks
before high-consequence planning. The first head needs a trusted provisioning
channel; an unwitnessed complete-history replacement is not detectable.

Use the [recovery](recovery.md), [spatial memory](spatial.md) and
[safety](safety.md) workflows for recovery trials, two-clock object maps and
retained safety evidence. LeRobot/RECAP-style value metadata exports are described
in [valuation](valuation.md).
