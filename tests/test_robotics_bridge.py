from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.test_gateway import AUTH, TOKEN
from tests.test_safety import protected_fact
from unimem import evidence, gateway, holdout_io, paths
from unimem import frontier_protocol as fp
from unimem import transparency_log as log
from unimem.robotics import bridge, edge, ingest


class LocalTransport:
    def __init__(self, root):
        self.gateway = gateway.Gateway(root, token=TOKEN)
    def post(self, path, body):
        response = self.gateway.handle("POST", path, AUTH, json.dumps(body).encode())
        if response.status != 200:
            raise ConnectionError("mock gateway rejected request")
        return json.loads(response.body)


def test_simulated_robot_loop_and_offline_store_forward(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="robotics")
    transport = LocalTransport(root)
    queue = edge.OutcomeQueue(str(tmp_path / "edge"))
    robot = bridge.RobotBridge(transport, embodiment="jaw", site="depot", skill="grasp", robot="r7", outcome_queue=queue)
    for i in range(120):
        result = robot.start(str(i), "grasp", items=[{"id": "recovery", "text": "Check the grasp pose"}])
        robot.finish(str(i), bool(result["deliver"]))
    assert evidence.for_lessons(root)["by_lesson"]["recovery"]["effect"] == 1
    assert not queue.pending()
    class Offline:
        def post(self, path, body):
            raise ConnectionError("offline")
    robot.transport = Offline()
    assert robot.finish("offline", True)["queued"]
    restarted = edge.OutcomeQueue(str(tmp_path / "edge"))
    assert restarted.flush(transport.post) == 1 and not restarted.pending()
    with pytest.raises(ValueError, match="conflicting"):
        restarted.enqueue({"occasion_id": "offline", "succeeded": False, "agent_id": "r7"})


def test_robot_bridge_fails_closed_without_memory_linked_shield_verdict():
    class Transport:
        def post(self, path, body):
            return {"deliver": [{"id": "clearance-rule", "text": "keep clearance"}]}

    robot = bridge.RobotBridge(Transport(), embodiment="arm", site="site", skill="pick", robot="r9")
    robot.start("episode-9", "pick")
    held = robot.authorize_action("episode-9", {"grasp": "close"}, spec_id="clearance", trace=[], monitor=None)
    assert held["allowed"] is False
    seen = {}

    def monitor(body):
        seen.update(body)
        return {"verdict": "safe", "action": "continue", "lineage_id": "verdict-1"}

    allowed = robot.authorize_action("episode-9", {"grasp": "close"}, spec_id="clearance",
                                     trace=[{"t": 0, "distance": 1}], monitor=monitor)
    assert allowed["allowed"] and seen["source_memory_ids"] == ["clearance-rule"]
    assert allowed["lineage_id"] == "verdict-1"


def test_ros_node_mock_drives_gateway_callbacks(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="ros")
    class Message:
        def __init__(self, data=""):
            self.data = data
    class Publisher:
        messages = []
        def publish(self, message):
            self.messages.append(message.data)
    class Node:
        def __init__(self, name):
            self.callbacks = {}
            self.publisher = Publisher()
        def create_publisher(self, message_type, topic, qos):
            return self.publisher
        def create_subscription(self, message_type, topic, callback, qos):
            self.callbacks[topic] = callback
    robot = bridge.RobotBridge(LocalTransport(root), embodiment="jaw", site="depot", skill="grasp", robot="r7")
    scene = []
    node = bridge.create_node(robot, node_factory=Node, message_type=Message, on_scene=scene.append)
    node.callbacks["/task/start"](Message(json.dumps({"occasion_id": "task", "query": "grasp"})))
    node.callbacks["/task/end"](Message(json.dumps({"occasion_id": "task", "succeeded": True})))
    node.callbacks["/scene/deltas"](Message('{"object_id":"crate"}'))
    assert node.publisher.messages and holdout_io.read_outcomes(root)["task"] and scene


def test_edge_cache_rejects_unsigned_and_forked_heads(tmp_path):
    root = str(tmp_path / "origin")
    hazard = protected_fact(root)
    first = edge.publish_update(root, [hazard.id])
    cache = edge.EdgeCache(str(tmp_path / "cache"), root)
    cache.update(first)
    assert cache.recall()[0]["hazard"]
    cache.update(first)
    assert len(fp.journal(str(tmp_path / "cache"), "edge-cache.jsonl")) == 1
    with pytest.raises(PermissionError):
        cache.update({"manifest": {}, "anchor": {}})
    second = edge.publish_update(root, [], previous_size=first["anchor"]["head"]["record"]["tree_size"])
    cache.update(second)
    assert cache.recall()[0]["id"] == hazard.id
    fork = str(tmp_path / "fork")
    Path(paths.memory_dir(fork)).mkdir(parents=True)
    shutil.copy(Path(paths.memory_dir(root)) / ".origin-key", Path(paths.memory_dir(fork)) / ".origin-key")
    signed = fp.sign(fork, {"type": "edge-context-v1", "items": [], "issued_at": fp.now()})
    anchor = log.append(fork, "edge-context-update", {"manifest_digest": fp.digest(signed)})
    with pytest.raises(PermissionError, match="forked|stale"):
        cache.update({"manifest": signed, "anchor": anchor, "consistency": {"proof": []}})


def test_lerobot_and_mcap_metadata_ingest(tmp_path):
    rows = [{"episode_index": 1, "summary": "Recover from slip", "next.success": True,
             "sensors": {"grasp_force": 2.4}, "media_refs": ["sha256:" + "a" * 64]}]
    path = tmp_path / "episodes.jsonl"
    path.write_text(json.dumps(rows[0]) + "\n")
    result = ingest.lerobot_jsonl(str(tmp_path / "store"), str(path), environment="sim", robot="r7")
    assert result[0]["env"] == "sim"
    with pytest.raises(ValueError):
        ingest.ingest_records(str(tmp_path), [{**rows[0], "media_refs": ["http://raw-video"]}],
                              format="mcap", environment="sim", robot="r7")
    mcap = pytest.importorskip("mcap.writer")
    mpath = tmp_path / "robot.mcap"
    with mpath.open("wb") as stream:
        writer = mcap.Writer(stream)
        writer.start()
        channel = writer.register_channel(topic="/unimem/episode", message_encoding="json", schema_id=0)
        writer.add_message(channel_id=channel, log_time=1, publish_time=1, data=json.dumps(rows[0]).encode())
        writer.finish()
    assert ingest.mcap_file(str(tmp_path / "mcap-store"), str(mpath), environment="real", robot="r7")[0]["env"] == "real"


def test_protected_lesson_can_be_provisioned_to_edge(tmp_path):
    from unimem import safety
    pytest.importorskip("mcp")
    from tests.test_mcp_server import _write_lesson
    root = str(tmp_path / "origin")
    path = _write_lesson(root, "hazard", body="Stop motion when a human enters the work cell.")
    from unimem import frontmatter, lesson_admission
    fm, body = frontmatter.read(path)
    fm["approval_receipt"] = lesson_admission.issue(root, path, fm, body, actor="engineer")
    frontmatter.write(path, fm, body)
    review = safety.review(root, "hazard", reviewer="engineer", reason="verified human interlock")
    safety.mark(root, "hazard", "human-proximity", review)
    bundle = edge.publish_update(root, ["hazard"])
    cache = edge.EdgeCache(str(tmp_path / "edge"), root)
    cache.update(bundle)
    assert cache.recall()[0]["id"] == "hazard" and cache.recall()[0]["hazard"]


def test_long_lived_edge_journals_compact_without_losing_hazards_or_outcomes(tmp_path, monkeypatch):
    monkeypatch.setattr(edge, "COMPACT_AFTER", 3)
    monkeypatch.setattr(edge, "ROTATE_OUTCOMES_AFTER", 8)
    root = str(tmp_path / "origin")
    hazard = protected_fact(root)
    cache_root = str(tmp_path / "cache")
    cache = edge.EdgeCache(cache_root, root)
    bundle = edge.publish_update(root, [hazard.id])
    cache.update(bundle)
    for _ in range(7):  # later partial refreshes omit the hazard
        size = bundle["anchor"]["head"]["record"]["tree_size"]
        bundle = edge.publish_update(root, [], previous_size=size)
        cache.update(bundle)
    rows = fp.journal(cache_root, "edge-cache.jsonl")
    assert len(rows) <= 4 and rows[0]["kind"] == "rotation"
    assert [item["id"] for item in cache.recall()] == [hazard.id]
    stale = edge.publish_update(root, [], previous_size=0)
    stale["anchor"] = rows[1]["bundle"]["anchor"] if "bundle" in rows[1] else stale["anchor"]
    with pytest.raises(PermissionError):
        cache.update({**stale, "consistency": {"proof": ["00" * 32]}})
    queue_root = str(tmp_path / "queue")
    queue = edge.OutcomeQueue(queue_root)
    other_process = edge.OutcomeQueue(queue_root)
    for i in range(12):
        queue.enqueue({"occasion_id": f"o{i}", "succeeded": True, "agent_id": "r7"})
        if i != 11:
            queue.acknowledge(f"o{i}")
        # A second process with a retained checkpoint accepts the signed rotation.
        assert [row["occasion_id"] for row in other_process.pending()] == [f"o{i}"] * (i == 11)
    assert len(fp.journal(queue_root, "edge-outcomes.jsonl")) < 12
    assert [row["occasion_id"] for row in queue.pending()] == ["o11"]


def test_bridge_accepts_realistic_labels_and_contains_bad_ros_messages(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="labels")
    robot = bridge.RobotBridge(LocalTransport(root), embodiment="franka-panda-parallel-jaw", site="depot-7",
                               skill="pick-and-place", robot="r7")
    assert robot.context == "franka-panda-parallel-jaw/depot-7/pick-and-place"
    with pytest.raises(ValueError):
        bridge.RobotBridge(LocalTransport(root), embodiment="x" * 40, site="y" * 40, skill="z", robot="r7")
    errors = []

    class Logger:
        def error(self, message):
            errors.append(message)

    class Node:
        def __init__(self, name):
            self.callbacks = {}
        def create_publisher(self, message_type, topic, qos):
            return None
        def create_subscription(self, message_type, topic, callback, qos):
            self.callbacks[topic] = callback
        def get_logger(self):
            return Logger()

    class Message:
        def __init__(self, data=""):
            self.data = data

    node = bridge.create_node(robot, node_factory=Node, message_type=Message)
    node.callbacks["/task/start"](Message("{not json"))
    node.callbacks["/task/end"](Message(json.dumps({"occasion_id": "x", "succeeded": "yes"})))
    assert len(errors) == 2


def test_ros_stl_service_blocks_violating_action_and_contains_malformed_input(tmp_path):
    from types import SimpleNamespace

    from unimem.robotics import shield

    root = str(tmp_path)
    shield.register(root, "clearance", memory_id="stop-memory", embodiment="arm",
                    description="maintain clearance", formula={
                        "op": "always", "from": 0, "to": 1,
                        "arg": {"op": "predicate", "signal": "distance", "cmp": ">=", "value": 0.5},
                    })
    errors, services = [], {}

    class Logger:
        def error(self, message):
            errors.append(message)

    class Node:
        def __init__(self, name):
            pass
        def create_publisher(self, message_type, topic, qos):
            return None
        def create_subscription(self, message_type, topic, callback, qos):
            pass
        def create_service(self, service_type, topic, callback):
            services[topic] = callback
        def get_logger(self):
            return Logger()

    class Message:
        def __init__(self, data=""):
            self.data = data

    robot = bridge.RobotBridge(LocalTransport(root), embodiment="arm", site="site", skill="grasp", robot="r7")
    bridge.create_node(robot, node_factory=Node, message_type=Message, on_shield=bridge.shield_service_handler(root),
                       shield_service_type=object)
    request = SimpleNamespace(request_json=json.dumps({"spec_id": "clearance", "action": {"grasp": True},
        "source_memory_ids": ["stop-memory"], "trace": [{"t": 0, "distance": 0.8}, {"t": 1, "distance": 0.2}]}))
    response = services[f"/unimem/robot_{fp.digest('r7')[:16]}/shield"](request, SimpleNamespace())
    assert not response.allowed and json.loads(response.verdict_json)["verdict"] == "violated"
    malformed = services[f"/unimem/robot_{fp.digest('r7')[:16]}/shield"](
        SimpleNamespace(request_json="not-json"), SimpleNamespace())
    assert not malformed.allowed and malformed.error == "JSONDecodeError" and errors
