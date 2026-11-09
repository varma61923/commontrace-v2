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
    holdout_io.configure(root, rate=0.5, salt="robotics")
    transport = LocalTransport(root)
    queue = edge.OutcomeQueue(str(tmp_path / "edge"))
    robot = bridge.RobotBridge(
        transport, embodiment="jaw", site="depot", skill="grasp", robot="r7", outcome_queue=queue
    )
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

    allowed = robot.authorize_action(
        "episode-9", {"grasp": "close"}, spec_id="clearance", trace=[{"t": 0, "distance": 1}], monitor=monitor
    )
    assert allowed["allowed"] and seen["source_memory_ids"] == ["clearance-rule"]
    assert allowed["lineage_id"] == "verdict-1"


def test_ros_node_mock_drives_gateway_callbacks(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="ros")

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
    rows = [
        {
            "episode_index": 1,
            "summary": "Recover from slip",
            "next.success": True,
            "sensors": {"grasp_force": 2.4},
            "media_refs": ["sha256:" + "a" * 64],
        }
    ]
    path = tmp_path / "episodes.jsonl"
    path.write_text(json.dumps(rows[0]) + "\n")
    result = ingest.lerobot_jsonl(str(tmp_path / "store"), str(path), environment="sim", robot="r7")
    assert result[0]["env"] == "sim"
    with pytest.raises(ValueError):
        ingest.ingest_records(
            str(tmp_path),
            [{**rows[0], "media_refs": ["http://raw-video"]}],
            format="mcap",
            environment="sim",
            robot="r7",
        )
    mcap = pytest.importorskip("mcap.writer")
    mpath = tmp_path / "robot.mcap"
    with mpath.open("wb") as stream:
        writer = mcap.Writer(stream)
        writer.start()
        channel = writer.register_channel(topic="/unimem/episode", message_encoding="json", schema_id=0)
        writer.add_message(channel_id=channel, log_time=1, publish_time=1, data=json.dumps(rows[0]).encode())
        writer.finish()
    assert (
        ingest.mcap_file(str(tmp_path / "mcap-store"), str(mpath), environment="real", robot="r7")[0]["env"] == "real"
    )


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
    holdout_io.configure(root, rate=0.5, salt="labels")
    robot = bridge.RobotBridge(
        LocalTransport(root), embodiment="franka-panda-parallel-jaw", site="depot-7", skill="pick-and-place", robot="r7"
    )
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
    shield.register(
        root,
        "clearance",
        memory_id="stop-memory",
        embodiment="arm",
        description="maintain clearance",
        formula={
            "op": "always",
            "from": 0,
            "to": 1,
            "arg": {"op": "predicate", "signal": "distance", "cmp": ">=", "value": 0.5},
        },
    )
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
    bridge.create_node(
        robot,
        node_factory=Node,
        message_type=Message,
        on_shield=bridge.shield_service_handler(root),
        shield_service_type=object,
    )
    request = SimpleNamespace(
        request_json=json.dumps(
            {
                "spec_id": "clearance",
                "action": {"grasp": True},
                "source_memory_ids": ["stop-memory"],
                "trace": [{"t": 0, "distance": 0.8}, {"t": 1, "distance": 0.2}],
            }
        )
    )
    response = services[f"/unimem/robot_{fp.digest('r7')[:16]}/shield"](request, SimpleNamespace())
    assert not response.allowed and json.loads(response.verdict_json)["verdict"] == "violated"
    malformed = services[f"/unimem/robot_{fp.digest('r7')[:16]}/shield"](
        SimpleNamespace(request_json="not-json"), SimpleNamespace()
    )
    assert not malformed.allowed and malformed.error == "JSONDecodeError" and errors


class _Refusing:
    """A gateway that definitively refuses one occasion and accepts the rest."""

    def __init__(self, refused):
        self.refused, self.accepted = refused, []

    def post(self, path, body):
        if body["occasion_id"] == self.refused:
            raise bridge.GatewayRefused(409, "conflicting_outcome")
        self.accepted.append(body["occasion_id"])
        return {"recorded": True}


def test_a_refused_outcome_never_blocks_the_outcomes_queued_after_it(tmp_path):
    queue = edge.OutcomeQueue(str(tmp_path))
    for occasion in ("t1", "t2", "t3"):
        queue.enqueue({"occasion_id": occasion, "reward": 0.5})
    gateway_ = _Refusing("t2")
    assert queue.flush(gateway_.post) == 2 and gateway_.accepted == ["t1", "t3"]
    assert queue.pending() == []
    [refused] = queue.rejected()
    assert refused == {
        "occasion_id": "t2",
        "status": 409,
        "code": "conflicting_outcome",
        "body": {"occasion_id": "t2", "reward": 0.5},
    }

    def offline(_path, _body):
        raise ConnectionError("offline")

    queue.enqueue({"occasion_id": "t4", "succeeded": True})
    with pytest.raises(ConnectionError):
        queue.flush(offline)
    assert [b["occasion_id"] for b in queue.pending()] == ["t4"]


def test_a_robot_learns_of_a_refusal_and_its_queue_settles_it(tmp_path):
    root = str(tmp_path / "store")
    holdout_io.configure(root, rate=0.5, salt="refusal")
    queue = edge.OutcomeQueue(str(tmp_path / "edge"))
    robot = bridge.RobotBridge(
        _Refusing("t1"), embodiment="arm", site="cell", skill="pick", robot="r1", outcome_queue=queue
    )
    with pytest.raises(bridge.GatewayRefused, match="409"):
        robot.finish("t1", reward=0.4)
    assert queue.pending() == [] and [r["occasion_id"] for r in queue.rejected()] == ["t1"]


def test_the_http_transport_tells_a_refusal_from_an_outage():
    import http.server
    import threading

    replies = {"/v1/outcome": (409, {"error": {"code": "conflicting_outcome", "message": "x"}})}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            status, body = replies["/v1/outcome"]
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        transport = bridge.HTTPGateway(f"http://127.0.0.1:{server.server_address[1]}", TOKEN)
        with pytest.raises(bridge.GatewayRefused) as refused:
            transport.post("/v1/outcome", {"occasion_id": "t1", "reward": 0.5})
        assert (refused.value.status, refused.value.code) == (409, "conflicting_outcome")
        for status in (401, 403, 404, 429, 503):
            replies["/v1/outcome"] = (status, {"error": {"code": "later"}})
            with pytest.raises(bridge.GatewayUnavailable, match="queued for retry") as unavailable:
                transport.post("/v1/outcome", {"occasion_id": "t1", "reward": 0.5})
            assert isinstance(unavailable.value, ConnectionError)
            assert (unavailable.value.status, unavailable.value.code) == (status, "later")
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("endpoint", ["https:///path", "https://host:0", "https://host:bad", "https://host:65536"])
def test_gateway_rejects_invalid_hosts_and_ports(endpoint):
    with pytest.raises(ValueError):
        bridge.HTTPGateway(endpoint, TOKEN)


@pytest.mark.parametrize("status", [200, 409, 503])
def test_gateway_bounds_body_reads_and_closes_responses(monkeypatch, status):
    from urllib.error import HTTPError

    from unimem import retry

    clock = [0.0]

    class Response:
        closed = False

        def read1(self, n):
            clock[0] += 0.6
            return b"x"

        def close(self):
            self.closed = True

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    response = Response()

    class Opener:
        def open(self, *_args, **_kwargs):
            if status != 200:
                raise HTTPError("https://gateway", status, "failure", {}, response)
            return response

    monkeypatch.setattr(bridge, "build_opener", lambda *_: Opener())
    monkeypatch.setattr(retry.time, "monotonic", lambda: clock[0])
    transport = bridge.HTTPGateway("https://gateway", TOKEN, timeout=1)
    with pytest.raises(bridge.GatewayRefused if status == 409 else ConnectionError):
        transport.post("/v1/outcome", {"occasion_id": "task", "reward": 1})
    assert response.closed and clock[0] == pytest.approx(1.2)


def test_a_replayed_outcome_after_a_power_loss_completes_through_the_gateway(tmp_path, monkeypatch):
    from unimem import rewards

    root = str(tmp_path / "store")
    holdout_io.configure(root, rate=0.5, salt="power-loss")
    queue = edge.OutcomeQueue(str(tmp_path / "edge"))
    queue.enqueue({"occasion_id": "t1", "reward": 0.6, "agent_id": "r1"})
    record_outcome = holdout_io.record_outcome

    def power_loss(*_args, **_kwargs):
        raise OSError("power lost between the reward and the outcome")

    monkeypatch.setattr(holdout_io, "record_outcome", power_loss)
    with pytest.raises(OSError):
        rewards.record(root, "t1", reward=0.6)
    monkeypatch.setattr(holdout_io, "record_outcome", record_outcome)
    assert rewards.audit(root).interrupted == ("t1",)
    assert queue.flush(LocalTransport(root).post) == 1
    assert queue.pending() == [] and rewards.audit(root).problems == ()
    assert holdout_io.read_outcomes(root)["t1"] == rewards.realize("power-loss", "t1", 0.6)
