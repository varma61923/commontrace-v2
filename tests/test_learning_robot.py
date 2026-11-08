"""A robot's graded outcomes remain pinned across disconnects and restarts."""

from __future__ import annotations

import pytest

from unimem.client import LEARNING_SCHEMA
from unimem.robotics.bridge import GatewayRefused, RobotBridge
from unimem.robotics.edge import OutcomeQueue


class Transport:
    def __init__(self):
        self.calls = []
        self.offline = False

    def get(self, path, context):
        if self.offline:
            raise ConnectionError("offline")
        return {"schema": LEARNING_SCHEMA, "operations": ["assign", "delivery", "outcome", "status"]}

    def post(self, path, body):
        self.calls.append((path, body))
        if self.offline:
            raise ConnectionError("offline")
        return {
            "schema": LEARNING_SCHEMA,
            "experiment_id": "experiment-1",
            "memories": [{"id": "m", "text": "safe", "revision": "r1"}],
        }


def bridge(transport, queue):
    return RobotBridge(transport, embodiment="arm", site="lab", skill="pick", robot="r1", outcome_queue=queue)


@pytest.mark.parametrize("reply", [None, [], True, "{}"])
def test_robot_rejects_non_object_negotiation_before_sending_assignment(tmp_path, monkeypatch, reply):
    transport = Transport()
    monkeypatch.setattr(transport, "get", lambda path, context: reply)
    robot = bridge(transport, OutcomeQueue(str(tmp_path)))
    with pytest.raises(ValueError, match="non-object"):
        robot.learning.assign("pick-1", ["m"])
    assert transport.calls == []
    assert robot._recalls == {}


def test_learning_robot_offline_replay_pins_experiment_and_route(tmp_path):
    transport = Transport()
    queue = OutcomeQueue(str(tmp_path))
    robot = bridge(transport, queue)
    assignment = robot.learning.assign("pick-1", ["m"])
    assert assignment["experiment_id"] == "experiment-1"
    assert robot._recalls["pick-1"]["deliver"] == []
    robot.learning.delivery("pick-1", ["m"])
    assert robot._recalls["pick-1"]["deliver"][0]["id"] == "m"
    with pytest.raises(ValueError, match="conflicts"):
        robot.learning.outcome("pick-1", reward=0.7, experiment_id="experiment-2")
    transport.offline = True
    assert robot.learning.outcome("pick-1", reward=0.7)["queued"]
    pending = OutcomeQueue(str(tmp_path)).pending()
    assert pending[0] == {
        "occasion_id": "pick-1",
        "experiment_id": "experiment-1",
        "reward": 0.7,
        "context": ["agent:r1"],
    }
    # Configuration changed while disconnected: replay must retain the old experiment.
    fresh = Transport()
    assert OutcomeQueue(str(tmp_path)).flush(fresh.post) == 1
    assert fresh.calls == [("/v1/learning/outcome", pending[0])]
    assert queue.pending() == []


def test_queue_keeps_legacy_and_learning_outcomes_distinct_and_conflicts_fail_closed(tmp_path):
    queue = OutcomeQueue(str(tmp_path))
    legacy = {"occasion_id": "legacy", "reward": 0.25}
    current = {"occasion_id": "new", "experiment_id": "e", "metrics": {"progress": 0.8}}
    queue.enqueue(legacy)
    queue.enqueue(current, path="/v1/learning/outcome")
    with pytest.raises(ValueError, match="conflicting"):
        queue.enqueue({**current, "experiment_id": "e2"}, path="/v1/learning/outcome")
    transport = Transport()
    assert queue.flush(transport.post) == 2
    assert transport.calls == [("/v1/outcome", legacy), ("/v1/learning/outcome", current)]
    with pytest.raises(ValueError, match="endpoint"):
        queue.enqueue({"occasion_id": "x", "succeeded": True}, path="https://attacker")


def test_learning_queue_does_not_acknowledge_incompatible_replies(tmp_path):
    queue = OutcomeQueue(str(tmp_path))
    queue.enqueue({"occasion_id": "o", "experiment_id": "e", "succeeded": True}, path="/v1/learning/outcome")
    with pytest.raises(RuntimeError, match="support"):
        queue.flush(lambda path, body: {"recorded": True})
    assert len(queue.pending()) == 1

    def refuse(path, body):
        raise GatewayRefused(409, "conflict")

    assert queue.flush(refuse) == 0
    assert queue.pending() == []
    assert queue.rejected()[0]["status"] == 409


def test_learning_robot_requires_explicit_pinning_after_restart(tmp_path):
    robot = bridge(Transport(), OutcomeQueue(str(tmp_path)))
    with pytest.raises(ValueError, match="pinned"):
        robot.learning.outcome("o", True)
    robot.learning.outcome("o", True, experiment_id="e")


def test_robot_replay_uses_real_assignment_time_utility_after_reconfiguration(tmp_path):
    from unimem import learning
    from unimem.client import MemoryClient
    from unimem.memory_journal import Journal
    from unimem.memory_record import MemoryRecord

    root = str(tmp_path)
    context = ["agent:r1"]
    Journal(root).put(
        MemoryRecord(id="m", kind="fact", version=1, payload={"text": "reduce force"}, scope={"agent": "r1"})
    )
    owner = MemoryClient(root, context={"agent": "r1"})
    configured = owner.learning.configure("observe", utility={"terms": {"progress": {"low": 0, "high": 100}}})

    class LocalTransport:
        offline = False

        def get(self, path, scopes):
            if self.offline:
                raise ConnectionError("offline")
            return learning.operate(root, path.rsplit("/", 1)[-1], {}, context=scopes, untrusted=True)

        def post(self, path, body):
            if self.offline:
                raise ConnectionError("offline")
            payload = {key: value for key, value in body.items() if key != "context"}
            return learning.operate(root, path.rsplit("/", 1)[-1], payload, context=body["context"], untrusted=True)

    transport = LocalTransport()
    queue = OutcomeQueue(root)
    robot = bridge(transport, queue)
    assigned = robot.learning.assign("pick", ["m"])
    robot.learning.delivery("pick", assigned["inject"])
    # The returned assignment is caller-owned; modifying it cannot unpin the SDK.
    assigned["experiment_id"] = "tampered"
    transport.offline = True
    assert robot.learning.outcome("pick", metrics={"progress": 50})["queued"]
    assert queue.pending()[0]["experiment_id"] == configured["experiment_id"]
    owner.learning.configure("observe", utility={"terms": {"progress": {"low": 0, "high": 200}}})
    transport.offline = False
    assert OutcomeQueue(root).flush(transport.post) == 1
    replayed = owner.learning.outcome("pick", metrics={"progress": 50}, experiment_id=configured["experiment_id"])
    assert replayed["outcome"]["reward"] == 0.5
    assert owner.learning.status(experiment_id=configured["experiment_id"])["occasions"] == 1
