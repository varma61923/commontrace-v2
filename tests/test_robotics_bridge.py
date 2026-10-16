from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from commontrace import evidence, gateway, holdout_io, paths
from commontrace import frontier_protocol as fp
from commontrace import transparency_log as log
from commontrace.robotics import bridge, edge, ingest
from tests.test_gateway import AUTH, TOKEN
from tests.test_safety import protected_fact


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
        channel = writer.register_channel(topic="/commontrace/episode", message_encoding="json", schema_id=0)
        writer.add_message(channel_id=channel, log_time=1, publish_time=1, data=json.dumps(rows[0]).encode())
        writer.finish()
    assert ingest.mcap_file(str(tmp_path / "mcap-store"), str(mpath), environment="real", robot="r7")[0]["env"] == "real"


def test_protected_lesson_can_be_provisioned_to_edge(tmp_path):
    from commontrace import safety
    pytest.importorskip("mcp")
    from tests.test_mcp_server import _write_lesson
    root = str(tmp_path / "origin")
    path = _write_lesson(root, "hazard", body="Stop motion when a human enters the work cell.")
    from commontrace import frontmatter, lesson_admission
    fm, body = frontmatter.read(path)
    fm["approval_receipt"] = lesson_admission.issue(root, path, fm, body, actor="engineer")
    frontmatter.write(path, fm, body)
    review = safety.review(root, "hazard", reviewer="engineer", reason="verified human interlock")
    safety.mark(root, "hazard", "human-proximity", review)
    bundle = edge.publish_update(root, ["hazard"])
    cache = edge.EdgeCache(str(tmp_path / "edge"), root)
    cache.update(bundle)
    assert cache.recall()[0]["id"] == "hazard" and cache.recall()[0]["hazard"]
