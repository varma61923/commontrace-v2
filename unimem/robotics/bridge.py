"""Planner-level recall/act/outcome bridge with a lazy ROS 2 node adapter."""
from __future__ import annotations

import json
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from unimem import frontier_protocol as fp


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
                 outcome_queue: Any = None, prewarm: Callable[[str], object] | None = None) -> None:
        from unimem import holdout_io

        self.transport = transport
        self.robot = fp.text(robot, "robot", 64)
        parts = [fp.text(x, "context component", 64) for x in (embodiment, site, skill)]
        if any("/" in part for part in parts):
            raise ValueError("embodiment, site and skill labels must not contain '/'")
        # The gateway's occasion context label: normalized and at most 64 characters.
        self.context = holdout_io.context_label("/".join(parts))
        self.queue = outcome_queue
        self._prewarm = prewarm
        self._recalls: dict[str, dict[str, Any]] = {}
        if prewarm is not None:
            prewarm("robot-boot")

    def start(self, occasion: str, query: str, *, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        fp.text(occasion)
        fp.text(query, "planner query", 4096)
        body: dict[str, Any] = {"occasion_id": occasion, "query": query, "agent_id": self.robot,
                                "context": self.context}
        if items is not None:
            fp.count(len(items), 0, 200)
            body["items"] = items
        if self._prewarm is not None:
            self._prewarm("task-assignment")
        result = self.transport.post("/v1/recall", body)
        if isinstance(result, dict):
            self._recalls[occasion] = result
            if len(self._recalls) > 128:
                self._recalls.pop(next(iter(self._recalls)))
        return result

    def authorize_action(self, occasion: str, action: dict[str, Any], *, spec_id: str,
                         trace: list[dict[str, Any]], monitor: Callable[[dict[str, Any]], dict[str, Any]] | None,
                         recovery_context: dict[str, str] | None = None,
                         failure_type: str | None = None) -> dict[str, Any]:
        """Fail closed unless an action is checked against memory recalled this occasion."""
        fp.text(occasion, "occasion", 128)
        if not isinstance(action, dict) or not action:
            raise ValueError("robot action must be a non-empty JSON object")
        fp.wire(action)
        recall = self._recalls.get(occasion)
        memories = recall.get("deliver", []) if recall else []
        source_ids = sorted({row["id"] for row in memories if isinstance(row, dict) and isinstance(row.get("id"), str)})
        if monitor is None or not source_ids:
            return {"allowed": False, "reason": "shield is not configured or no memory was recalled",
                    "verdict": "hold"}
        request: dict[str, Any] = {"spec_id": spec_id, "action": action, "trace": trace,
                                   "source_memory_ids": source_ids}
        if recovery_context is not None:
            request["recovery_context"] = recovery_context
        if failure_type is not None:
            request["failure_type"] = failure_type
        result = monitor(request)
        if not isinstance(result, dict):
            return {"allowed": False, "reason": "invalid monitor response", "verdict": "hold"}
        allowed = result.get("verdict") == "safe" and result.get("action") == "continue"
        return {"allowed": allowed, "verdict": result.get("verdict", "invalid-monitor-response"),
                "lineage_id": result.get("lineage_id"), "monitor": result}

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
        self._recalls.pop(occasion, None)
        return result


def create_node(bridge: RobotBridge, *, topics: dict[str, str] | None = None, node_factory: Any = None,
                message_type: Any = None, on_failure: Callable[[dict[str, Any]], Any] | None = None,
                on_scene: Callable[[dict[str, Any]], Any] | None = None,
                on_ask: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                service_type: Any = None, on_shield: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                shield_service_type: Any = None) -> Any:
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
            raise RuntimeError("install ROS 2 rclpy/std_msgs and unimem[robotics] to start the ROS node") from None
        node_factory, message_type = Node, String
    config = {"start": "/task/start", "end": "/task/end", "success": "/task/success",
              "failure": "/task/failure", "scene": "/scene/deltas", "memories": "/planner/memories",
              "ask_service": "/unimem/robot_"+fp.digest(bridge.robot)[:16]+"/should_ask",
              "shield_service": "/unimem/robot_"+fp.digest(bridge.robot)[:16]+"/shield", **(topics or {})}
    for topic in config.values():
        fp.text(topic, "ROS topic", 256)
    node = node_factory("unimem_memory")
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
                    logger().error(f"unimem memory callback rejected a message: {type(exc).__name__}")
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
    if on_ask is not None:
        if service_type is None:
            try:
                from unimem_interfaces.srv import ShouldAsk
            except ImportError:
                raise RuntimeError(
                    "build and source ros2/unimem_interfaces to enable the ShouldAsk service") from None
            service_type = ShouldAsk

        def should_ask_service(request: Any, response: Any) -> Any:
            try:
                occasion = fp.text(request.occasion, "ROS asking occasion", 128)
                question_type = fp.text(request.question_type, "ROS question type", 64)
                result = on_ask({"occasion": occasion, "question_type": question_type, "context": bridge.context})
                encoded = fp.wire(result)
                if len(encoded) > 8192:
                    raise ValueError("asking service response exceeds 8 KiB")
                response.ok, response.decision_json, response.error = True, encoded.decode(), ""
            except Exception as exc:  # noqa: BLE001 - fail closed without stopping the ROS executor
                response.ok = False
                response.decision_json = '{"decision":"which-mode","mode":"safe-escalation","randomized":false}'
                response.error = type(exc).__name__
            return response

        node.create_service(service_type, config["ask_service"], should_ask_service)
    if on_shield is not None:
        if shield_service_type is None:
            try:
                from unimem_interfaces.srv import CheckShield
            except ImportError:
                raise RuntimeError(
                    "build and source ros2/unimem_interfaces to enable the shield service") from None
            shield_service_type = CheckShield

        def shield_service(request: Any, response: Any) -> Any:
            try:
                if not isinstance(request.request_json, str) or len(request.request_json.encode()) > 8192:
                    raise ValueError("STL shield request exceeds 8 KiB")
                body = json.loads(request.request_json)
                fp.wire(body)
                if not isinstance(body, dict):
                    raise ValueError("STL shield request must be an object")
                result = on_shield(body)
                encoded = fp.wire(result)
                if len(encoded) > 8192:
                    raise ValueError("STL shield response exceeds 8 KiB")
                response.allowed = result.get("action") == "continue"
                response.verdict_json = encoded.decode()
                response.error = ""
            except Exception as exc:  # noqa: BLE001 - fail closed without stopping the ROS executor
                response.allowed = False
                response.verdict_json = '{"verdict":"monitor-error","action":"hold"}'
                response.error = type(exc).__name__
                logger = getattr(node, "get_logger", None)
                if callable(logger):
                    logger().error(f"unimem STL service rejected a request: {type(exc).__name__}")
            return response

        node.create_service(shield_service_type, config["shield_service"], shield_service)
    return node


def household_scene_handler(root: str, *, room_resolver: Callable[[dict[str, Any]], str],
                            robot_identity_resolver: Callable[[dict[str, Any]], list[dict[str, Any]]],
                            persist: Callable[[dict[str, Any], list[dict[str, str]], dict[str, Any]], Any]
                            ) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Bind robot-stack identity and room signals to the household write gate."""
    from unimem.household import bridge_scene_handler

    return bridge_scene_handler(root, room_resolver=room_resolver,
                                identity_resolver=robot_identity_resolver, persist=persist)


def signature_sample_handler(root: str, *, skill: str, embodiment: str, site: str,
                             scopes: list[str] | None = None) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Bind ROS/MCAP sensor samples to the force-signature anomaly registry."""
    from unimem.robotics import signatures

    def handle(sample: dict[str, Any]) -> dict[str, Any]:
        return signatures.check(root, sample, skill=skill, embodiment=embodiment,
                                site=site, scopes=scopes or [])
    return handle


def shield_service_handler(root: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Bind the ROS shield service to the bounded temporal monitor."""
    from unimem.robotics import shield

    def handle(request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if not isinstance(action, dict):
            raise ValueError("shield service requires the proposed robot action")
        return shield.check(root, request["spec_id"], action=action, trace=request["trace"],
                            source_memory_ids=request["source_memory_ids"],
                            recovery_memory_ids=request.get("recovery_memory_ids"),
                            recovery_context=request.get("recovery_context"),
                            failure_type=request.get("failure_type"))
    return handle


def working_memory_node(memory: Any, *, root: str | None = None, node_factory: Any = None, message_type: Any = None,
                        topics: dict[str, str] | None = None) -> Any:
    """Optional ROS 2 endpoint for local keyframe add/retrieve/consolidate operations."""
    if node_factory is None or message_type is None:
        try:
            from rclpy.node import Node
            from std_msgs.msg import String
        except ImportError:
            raise RuntimeError("install ROS 2 rclpy/std_msgs to start the working-memory endpoint") from None
        node_factory, message_type = Node, String
    config = {"add": "/unimem/working_memory/add", "query": "/unimem/working_memory/query",
              "consolidate": "/unimem/working_memory/consolidate", **(topics or {})}
    node = node_factory("unimem_working_memory")
    publisher = node.create_publisher(message_type, "/unimem/working_memory/result", 10)

    def subscribe(topic: str, operation: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        def callback(message: Any) -> None:
            try:
                if not isinstance(message.data, str) or len(message.data.encode()) > 8192:
                    raise ValueError("working-memory ROS envelope exceeds 8 KiB")
                body = json.loads(message.data)
                fp.wire(body)
                if not isinstance(body, dict):
                    raise ValueError("working-memory ROS message must be an object")
                result = operation(body)
            except Exception as exc:  # noqa: BLE001 - malformed edge input cannot stop the ROS executor
                result = {"error": type(exc).__name__}
                logger = getattr(node, "get_logger", None)
                if callable(logger):
                    logger().error(f"unimem working-memory callback rejected a message: {type(exc).__name__}")
            response = message_type()
            response.data = fp.wire(result).decode()
            publisher.publish(response)
        node.create_subscription(message_type, topic, callback, 10)

    subscribe(config["add"], lambda body: memory.add(**body))
    subscribe(config["query"], lambda body: memory.retrieve(**body))
    if root is not None:
        subscribe(config["consolidate"], lambda body: memory.consolidate(
            root, body["episode_id"], scopes=body.get("scopes", [])))
    return node
