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


def test_assignment_retry_preserves_acknowledged_delivery(tmp_path):
    robot = bridge(Transport(), OutcomeQueue(str(tmp_path)))
    robot.learning.assign("task", ["m"])
    robot.learning.delivery("task", ["m"])
    delivered = robot._recalls["task"]
    robot.learning.assign("task", ["m"])
    assert robot._recalls["task"] == delivered
    assert robot._recalls["task"]["deliver"][0]["id"] == "m"


def test_locally_durable_outcome_releases_slots_and_action_context(tmp_path):
    transport = Transport()
    queue = OutcomeQueue(str(tmp_path))
    robot = bridge(transport, queue)
    robot.learning.assign("task", ["m"])
    robot.learning.delivery("task", ["m"])
    transport.offline = True
    assert robot.learning.outcome("task", True)["queued"]
    assert robot.learning._assignments == {}
    assert robot._recalls == {}
    assert len(queue.pending()) == 1


def test_learning_queue_roundtrips_character_bounded_unicode_identity(tmp_path):
    queue = OutcomeQueue(str(tmp_path))
    identity = "🦾" * 200
    body = {"occasion_id": identity, "experiment_id": "e", "succeeded": True}
    queue.enqueue(body, path="/v1/learning/outcome")
    assert queue.flush(lambda path, body: {"schema": LEARNING_SCHEMA}) == 1
    assert queue.pending() == []


def test_learning_outcomes_cannot_be_settled_by_a_pre_rotation_reply(tmp_path, monkeypatch):
    from unimem.robotics import edge

    queue = OutcomeQueue(str(tmp_path))
    original = {"occasion_id": "task", "experiment_id": "e", "reward": 0.25}
    replacement = {**original, "reward": 0.75}
    key = queue.enqueue(original, path="/v1/learning/outcome")
    monkeypatch.setattr(edge, "ROTATE_OUTCOMES_AFTER", 1)

    def delayed_reply(path, body):
        # Another flusher has already settled this generation and rotated it.
        queue.acknowledge("task", queue_id=key)
        queue.enqueue(replacement, path=path)
        return {"schema": LEARNING_SCHEMA}

    assert queue.flush(delayed_reply) == 1
    assert queue.pending() == [replacement]


def test_request_inputs_cannot_change_during_capability_negotiation(tmp_path, monkeypatch):
    transport = Transport()
    robot = bridge(transport, OutcomeQueue(str(tmp_path)))
    ids = ["m"]
    original = transport.get

    def negotiate(path, context):
        ids.append("not-selected")
        return original(path, context)

    monkeypatch.setattr(transport, "get", negotiate)
    robot.learning.assign("task", ids)
    assert transport.calls[-1][1]["memory_ids"] == ["m"]


def test_delivery_bookkeeping_uses_the_ids_actually_sent(tmp_path, monkeypatch):
    transport = Transport()
    robot = bridge(transport, OutcomeQueue(str(tmp_path)))
    robot.learning.assign("task", ["m"])
    ids = ["m"]
    original = transport.get

    def negotiate(path, context):
        ids.clear()
        return original(path, context)

    monkeypatch.setattr(transport, "get", negotiate)
    robot.learning.delivery("task", ids)
    assert transport.calls[-1][1]["memory_ids"] == ["m"]
    assert robot._recalls["task"]["deliver"][0]["id"] == "m"


def test_concurrent_assignments_reserve_capacity_before_network_io(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()

    class BlockingTransport(Transport):
        def post(self, path, body):
            if path.endswith("assign") and body["occasion_id"] == "pending":
                entered.set()
                assert release.wait(5)
            return super().post(path, body)

    transport = BlockingTransport()
    robot = bridge(transport, OutcomeQueue(str(tmp_path)))
    for index in range(127):
        robot.learning.assign(str(index), ["m"])
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(robot.learning.assign, "pending", ["m"])
        try:
            assert entered.wait(5)
            with pytest.raises(ValueError, match="too many unfinished"):
                robot.learning.assign("overflow", ["m"])
            with pytest.raises(ValueError, match="operation is in progress"):
                robot.learning.outcome("pending", reward=1, experiment_id="experiment-1")
            # An unrelated episode can finish while another request is blocked.
            robot.learning.outcome("0", reward=1)
            robot.learning.assign("replacement", ["m"])
        finally:
            release.set()
        assert pending.result()["experiment_id"] == "experiment-1"
    assert len(robot.learning._assignments) == 128
    assert "overflow" not in robot.learning._assignments


def test_legacy_and_learning_tasks_cannot_replace_each_others_action_context(tmp_path):
    robot = bridge(Transport(), OutcomeQueue(str(tmp_path)))
    robot.learning.assign("learning", ["m"])
    robot.learning.delivery("learning", ["m"])
    with pytest.raises(ValueError, match="active learning"):
        robot.start("learning", "pick")
    with pytest.raises(ValueError, match="active learning"):
        robot.finish("learning", True)
    robot.start("legacy", "pick")
    with pytest.raises(ValueError, match="active legacy"):
        robot.learning.assign("legacy", ["m"])
    assert robot._recalls["learning"]["deliver"][0]["id"] == "m"


def test_restarted_robot_can_deliver_and_finish_with_an_explicit_experiment(tmp_path):
    robot = bridge(Transport(), OutcomeQueue(str(tmp_path)))
    robot.learning.delivery("resumed", ["m"], experiment_id="experiment-1")
    with pytest.raises(ValueError, match="active learning"):
        robot.finish("resumed", True)
    robot.learning.outcome("resumed", reward=1, experiment_id="experiment-1")
    assert not robot._recalls and not robot._slots


def test_assignment_reply_cannot_change_the_requested_experiment(tmp_path, monkeypatch):
    transport = Transport()
    robot = bridge(transport, OutcomeQueue(str(tmp_path)))
    with pytest.raises(ValueError, match="changed the requested experiment"):
        robot.learning.assign("task", ["m"], experiment_id="other")
    assert not robot._recalls and not robot.learning._assignments and not robot._slots
    robot.learning.assign("task", ["m"])
    robot.learning.delivery("task", ["m"])
    monkeypatch.setattr(transport, "post", lambda *_: {"schema": LEARNING_SCHEMA, "experiment_id": "other"})
    with pytest.raises(ValueError, match="unfinished robot assignment"):
        robot.learning.assign("task", ["m"])
    assert robot._recalls["task"]["deliver"][0]["id"] == "m"
    assert robot.learning._assignments["task"]["experiment_id"] == "experiment-1"


def test_capability_reply_mutation_cannot_disable_outcome_support(tmp_path):
    robot = bridge(Transport(), OutcomeQueue(str(tmp_path)))
    robot.learning.assign("task", ["m"])
    capabilities = robot.learning.capabilities()
    capabilities["operations"].clear()
    assert robot.learning.outcome("task", reward=1)["schema"] == LEARNING_SCHEMA


def test_offline_legacy_completion_releases_action_context(tmp_path, monkeypatch):
    transport = Transport()
    monkeypatch.setattr(transport, "post", lambda *_: {"deliver": [{"id": "m"}]})
    robot = bridge(transport, OutcomeQueue(str(tmp_path)))
    recalled = robot.start("task", "pick")
    recalled["deliver"].clear()

    def monitor(_):
        return {"verdict": "safe", "action": "continue"}

    assert robot.authorize_action("task", {"move": 1}, spec_id="s", trace=[], monitor=monitor)["allowed"]

    def offline(*_):
        raise ConnectionError("offline")

    monkeypatch.setattr(transport, "post", offline)
    assert robot.finish("task", True)["queued"]
    assert not robot._slots
    assert not robot.authorize_action("task", {"move": 1}, spec_id="s", trace=[], monitor=monitor)["allowed"]


def test_flush_batches_journal_validation_and_preserves_a_completed_prefix(tmp_path, monkeypatch):
    queue = OutcomeQueue(str(tmp_path))
    for index in range(70):
        queue.enqueue({"occasion_id": str(index), "succeeded": True})
    original = queue._state
    validations = []

    def state():
        validations.append(True)
        return original()

    monkeypatch.setattr(queue, "_state", state)

    def post(path, body):
        if body["occasion_id"] == "35":
            raise ConnectionError("offline")
        return {"recorded": True}

    with pytest.raises(ConnectionError, match="offline"):
        queue.flush(post)
    assert len(validations) == 3  # Snapshot, full batch, completed tail before timeout.
    assert [body["occasion_id"] for body in OutcomeQueue(str(tmp_path)).pending()] == [str(i) for i in range(35, 70)]
    validations.clear()
    assert queue.flush(lambda *_: {"recorded": True}) == 35
    assert len(validations) == 3
    assert OutcomeQueue(str(tmp_path)).pending() == []


def test_queue_keeps_legacy_and_learning_outcomes_distinct_and_conflicts_fail_closed(tmp_path):
    queue = OutcomeQueue(str(tmp_path))
    legacy = {"occasion_id": "legacy", "reward": 0.25}
    current = {"occasion_id": "new", "experiment_id": "e", "metrics": {"progress": 0.8}}
    queue.enqueue(legacy)
    queue.enqueue(current, path="/v1/learning/outcome")
    with pytest.raises(ValueError, match="conflicting"):
        queue.enqueue({**current, "metrics": {"progress": 0.2}}, path="/v1/learning/outcome")
    transport = Transport()
    assert queue.flush(transport.post) == 2
    assert transport.calls == [("/v1/outcome", legacy), ("/v1/learning/outcome", current)]
    with pytest.raises(ValueError, match="endpoint"):
        queue.enqueue({"occasion_id": "x", "succeeded": True}, path="https://attacker")


def test_queue_identity_pins_route_experiment_and_scope_without_cross_acknowledgement(tmp_path):
    from unimem.robotics.edge import OutcomeConflict

    queue = OutcomeQueue(str(tmp_path))
    bodies = [
        {"occasion_id": "same", "experiment_id": "e1", "context": ["agent:r1"], "reward": 0.5},
        {"occasion_id": "same", "experiment_id": "e2", "context": ["agent:r1"], "reward": 0.5},
        {"occasion_id": "same", "experiment_id": "e2", "context": ["agent:r2"], "reward": 0.5},
    ]
    keys = [queue.enqueue(body, path="/v1/learning/outcome") for body in bodies]
    legacy = {"occasion_id": "same", "succeeded": True}
    keys.append(queue.enqueue(legacy))
    assert len(set(keys)) == 4
    with pytest.raises(OutcomeConflict, match="ambiguous"):
        queue.acknowledge("same")
    queue.acknowledge("same", queue_id=keys[0])
    assert queue.pending() == [*bodies[1:], legacy]
    calls = []

    def post(path, body):
        calls.append((path, body))
        if body == bodies[1]:
            raise GatewayRefused(409, "conflict")
        return {"schema": LEARNING_SCHEMA}

    assert queue.flush(post) == 2
    assert queue.pending() == []
    assert queue.rejected()[0]["body"] == bodies[1]
    assert [body for _, body in calls] == [*bodies[1:], legacy]


def test_preupgrade_acknowledgement_cannot_settle_a_later_experiment(tmp_path):
    from unimem import frontier_protocol as fp

    root = str(tmp_path)
    old = {"occasion_id": "same", "experiment_id": "old", "reward": 0.5}
    fp.append(
        root,
        "edge-outcomes.jsonl",
        {"kind": "enqueue", "occasion_id": "same", "body": old, "path": "/v1/learning/outcome"},
    )
    fp.append(root, "edge-outcomes.jsonl", {"kind": "ack", "occasion_id": "same"})
    queue = OutcomeQueue(root)
    current = {**old, "experiment_id": "new"}
    queue.enqueue(current, path="/v1/learning/outcome")
    assert OutcomeQueue(root).pending() == [current]
    assert queue.flush(lambda path, body: {"schema": LEARNING_SCHEMA}) == 1
    assert queue.pending() == []


def test_learning_queue_canonicalizes_scope_order_for_retry_identity(tmp_path):
    queue = OutcomeQueue(str(tmp_path))
    body = {"occasion_id": "o", "experiment_id": "e", "context": ["agent:r", "project:p"], "succeeded": True}
    key = queue.enqueue(body, path="/v1/learning/outcome")
    assert queue.enqueue({**body, "context": list(reversed(body["context"]))}, path="/v1/learning/outcome") == key
    assert len(queue.pending()) == 1


def test_recovery_refuses_conflicting_historical_queue_entries(tmp_path):
    from unimem import frontier_protocol as fp
    from unimem.robotics.edge import OutcomeConflict

    root = str(tmp_path)
    for reward in (0.2, 0.8):
        fp.append(
            root,
            "edge-outcomes.jsonl",
            {
                "kind": "enqueue",
                "occasion_id": "same",
                "body": {"occasion_id": "same", "reward": reward},
                "path": "/v1/outcome",
            },
        )
    with pytest.raises(OutcomeConflict, match="history"):
        OutcomeQueue(root).pending()


def test_queue_freezes_caller_input_before_waiting_for_admission(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    queue = OutcomeQueue(str(tmp_path))
    entered, release = Event(), Event()
    original = queue._state

    def paused():
        entered.set()
        assert release.wait(5)
        return original()

    monkeypatch.setattr(queue, "_state", paused)
    body = {"occasion_id": "o", "experiment_id": "e1", "metrics": {"progress": 0.5}}
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(queue.enqueue, body, path="/v1/learning/outcome")
        try:
            assert entered.wait(5)
            body["experiment_id"] = "e2"
            body["metrics"]["progress"] = 0.9
        finally:
            release.set()
        pending.result(timeout=5)
    assert queue.pending() == [{"occasion_id": "o", "experiment_id": "e1", "metrics": {"progress": 0.5}}]


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
