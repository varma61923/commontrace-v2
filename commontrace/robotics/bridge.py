"""Planner-level recall/act/outcome bridge with a lazy ROS 2 node adapter."""
from __future__ import annotations

import json
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from commontrace import frontier_protocol as fp


class GatewayTransport(Protocol):
    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Any:
        raise ValueError("robot gateway redirects are refused")


class HTTPGateway:
    def __init__(self, endpoint: str, token: str, *, timeout: float = 10) -> None:
        fp.text(endpoint, "gateway endpoint", 2048)
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost"))
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("gateway requires HTTPS or loopback HTTP without embedded credentials")
        fp.text(token, "gateway token", 4096)
        self.endpoint, self._token = endpoint.rstrip("/"), token
        self.timeout = fp.number(timeout, .1, 60)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path not in ("/v1/recall", "/v1/outcome"):
            raise ValueError("robot bridge permits only recall and outcome gateway operations")
        req = Request(self.endpoint + path, fp.wire(body), method="POST",
                      headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        try:
            with build_opener(_NoRedirect()).open(req, timeout=self.timeout) as response:
                raw = response.read(fp.MAX_BYTES + 1)
            if len(raw) > fp.MAX_BYTES:
                raise ValueError("gateway response exceeds 1 MiB")
            value = json.loads(raw)
            fp.wire(value)
            if not isinstance(value, dict):
                raise ValueError("gateway returned a non-object")
            return value
        except Exception:
            raise ConnectionError("robot gateway request failed; outcome remains queued for retry") from None


class RobotBridge:
    def __init__(self, transport: GatewayTransport, *, embodiment: str, site: str, skill: str, robot: str,
                 outcome_queue: Any = None) -> None:
        from commontrace import holdout_io

        self.transport = transport
        self.robot = fp.text(robot, "robot", 64)
        parts = [fp.text(x, "context component", 64) for x in (embodiment, site, skill)]
        if any("/" in part for part in parts):
            raise ValueError("embodiment, site and skill labels must not contain '/'")
        # The gateway's occasion context label: normalized and at most 64 characters.
        self.context = holdout_io.context_label("/".join(parts))
        self.queue = outcome_queue

    def start(self, occasion: str, query: str, *, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        fp.text(occasion)
        fp.text(query, "planner query", 4096)
        body: dict[str, Any] = {"occasion_id": occasion, "query": query, "agent_id": self.robot,
                                "context": self.context}
        if items is not None:
            fp.count(len(items), 0, 200)
            body["items"] = items
        return self.transport.post("/v1/recall", body)

    def finish(self, occasion: str, succeeded: bool) -> dict[str, Any]:
        fp.text(occasion)
        if type(succeeded) is not bool:
            raise ValueError("robot success detector must supply a binary final outcome")
        body = {"occasion_id": occasion, "succeeded": succeeded, "agent_id": self.robot}
        if self.queue is not None:
            self.queue.enqueue(body)
        try:
            result = self.transport.post("/v1/outcome", body)
        except ConnectionError:
            if self.queue is None:
                raise
            return {"queued": True, "occasion_id": occasion}
        if self.queue is not None:
            self.queue.acknowledge(occasion)
        return result


def create_node(bridge: RobotBridge, *, topics: dict[str, str] | None = None, node_factory: Any = None,
                message_type: Any = None, on_failure: Callable[[dict[str, Any]], Any] | None = None,
                on_scene: Callable[[dict[str, Any]], Any] | None = None) -> Any:
    """Use std_msgs/String JSON envelopes; injectable node/message types keep CI ROS-free.

    Success-detector/task-end topics call finish. Failures and scene deltas are
    routed to explicitly configured scoped handlers (recovery.signature and
    spatial.update), never interpreted as motor commands.
    """
    if node_factory is None or message_type is None:
        try:
            from rclpy.node import Node
            from std_msgs.msg import String
        except ImportError:
            raise RuntimeError("install ROS 2 rclpy/std_msgs and commontrace[robotics] to start the ROS node") from None
        node_factory, message_type = Node, String
    config = {"start": "/task/start", "end": "/task/end", "success": "/task/success",
              "failure": "/task/failure", "scene": "/scene/deltas", "memories": "/planner/memories", **(topics or {})}
    for topic in config.values():
        fp.text(topic, "ROS topic", 256)
    node = node_factory("commontrace_memory")
    publisher = node.create_publisher(message_type, config["memories"], 10)

    def guarded(handler: Callable[[Any], None]) -> Callable[[Any], None]:
        # An exception escaping an rclpy callback stops the executor: one malformed
        # or undeliverable message must not take the robot's memory node down.
        def callback(message: Any) -> None:
            try:
                handler(message)
            except Exception as exc:  # noqa: BLE001 - reported, never raised into the executor
                logger = getattr(node, "get_logger", None)
                if callable(logger):
                    logger().error(f"commontrace memory callback rejected a message: {type(exc).__name__}")
        return callback

    def decode(message: Any) -> dict[str, Any]:
        if not isinstance(message.data, str) or len(message.data.encode()) > 8192:
            raise ValueError("ROS envelope exceeds 8 KiB")
        body = json.loads(message.data)
        fp.wire(body)
        if not isinstance(body, dict):
            raise ValueError("ROS envelope must be an object")
        return body

    def start(message: Any) -> None:
        body = decode(message)
        result = bridge.start(body["occasion_id"], body["query"])
        output = message_type()
        output.data = fp.wire(result).decode()
        publisher.publish(output)

    def end(message: Any) -> None:
        body = decode(message)
        bridge.finish(body["occasion_id"], body["succeeded"])

    node.create_subscription(message_type, config["start"], guarded(start), 10)
    node.create_subscription(message_type, config["end"], guarded(end), 10)
    node.create_subscription(message_type, config["success"], guarded(end), 10)
    if on_failure:
        node.create_subscription(message_type, config["failure"], guarded(lambda msg: on_failure(decode(msg))), 10)
    if on_scene:
        node.create_subscription(message_type, config["scene"], guarded(lambda msg: on_scene(decode(msg))), 10)
    return node
