"""Locally authored VDA 5050 3.0 interoperability subset for state and orders.

This validates only fields consumed by site memory; it is not a full standard
schema or conformance implementation.
"""
from __future__ import annotations

from typing import Any

from commontrace import frontier_protocol as fp

VERSION = "3.0.0"


def _header(message: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(message, dict):
        raise ValueError("VDA message must be an object")
    required = {"headerId", "timestamp", "version", "manufacturer", "serialNumber"}
    if not required <= set(message) or message["version"] != VERSION:
        raise ValueError("VDA 5050 interoperability subset requires version 3.0.0 header fields")
    fp.count(message["headerId"], 0, 2**63 - 1, "VDA header id")
    fp.moment(message["timestamp"])
    return {"header_id": message["headerId"], "timestamp": message["timestamp"],
            "manufacturer": fp.text(message["manufacturer"], "manufacturer", 128),
            "serial_number": fp.text(message["serialNumber"], "serial number", 128)}


def _array(value: Any, label: str, *, minimum: int = 0, maximum: int = 4096) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    fp.count(len(value), minimum, maximum, label)
    return value


def _zones(message: dict[str, Any]) -> list[str]:
    return sorted(set(fp.text(value, "zone id", 128)
                      for value in _array(message.get("zoneIds", []), "VDA zones", maximum=512)))


def _path_sharing(message: dict[str, Any], edge_ids: set[str]) -> list[str]:
    """Validate optional shared-edge references without retaining vendor payloads."""
    values = _array(message.get("pathSharing", []), "VDA path sharing", maximum=4096)
    shared = [fp.text(value, "shared edge id", 128) for value in values]
    if not set(shared) <= edge_ids:
        raise ValueError("VDA path sharing must reference edges in the same message")
    return shared


def validate_zone(message: dict[str, Any]) -> dict[str, Any]:
    """Validate a bounded zone-only envelope used by site adapters."""
    header = _header(message)
    if "zoneIds" not in message:
        raise ValueError("VDA zone subset requires zone IDs")
    return {**header, "zones": _zones(message)}


def validate_state(message: dict[str, Any]) -> dict[str, Any]:
    header = _header(message)
    if "orderId" not in message or "orderUpdateId" not in message:
        raise ValueError("VDA state subset requires order ID and update ID")
    nodes = _array(message.get("nodeStates", []), "VDA nodes")
    edges = _array(message.get("edgeStates", []), "VDA edges")
    normalized_nodes = []
    for node in nodes:
        if not isinstance(node, dict) or not {"nodeId", "sequenceId"} <= set(node):
            raise ValueError("VDA node state requires node ID and sequence ID")
        position = node.get("nodePosition")
        if position is not None:
            if not isinstance(position, dict) or not {"x", "y", "mapId"} <= set(position):
                raise ValueError("VDA position requires numeric xy and map ID")
            position = {"x": fp.number(position["x"], -1e7, 1e7),
                        "y": fp.number(position["y"], -1e7, 1e7),
                        "map_id": fp.text(position["mapId"], "map id", 128)}
        released = node.get("released", False)
        if type(released) is not bool:
            raise ValueError("VDA released flag must be boolean")
        normalized_nodes.append({"node_id": fp.text(node["nodeId"], "node id", 128),
            "sequence_id": fp.count(node["sequenceId"], 0, 2**31-1), "position": position,
            "released": released})
    normalized_edges = []
    edge_ids: set[str] = set()
    for edge in edges:
        if not isinstance(edge, dict) or not {"edgeId", "sequenceId", "startNodeId", "endNodeId"} <= set(edge):
            raise ValueError("VDA edge state requires edge ID, sequence and endpoints")
        released = edge.get("released", False)
        if type(released) is not bool:
            raise ValueError("VDA released flag must be boolean")
        edge_id = fp.text(edge["edgeId"], "edge id", 128)
        edge_ids.add(edge_id)
        normalized_edges.append({"edge_id": edge_id,
            "sequence_id": fp.count(edge["sequenceId"], 0, 2**31-1),
            "start": fp.text(edge["startNodeId"], "start node", 128),
            "end": fp.text(edge["endNodeId"], "end node", 128),
            "released": released})
    return {**header, "order_id": fp.text(message["orderId"], "order id", 128),
            "order_update_id": fp.count(message["orderUpdateId"], 0, 2**31-1),
            "nodes": normalized_nodes, "edges": normalized_edges,
            "zones": _zones(message), "path_sharing": _path_sharing(message, edge_ids)}


def validate_order(message: dict[str, Any]) -> dict[str, Any]:
    header = _header(message)
    if not {"orderId", "orderUpdateId", "nodes", "edges"} <= set(message):
        raise ValueError("VDA order subset requires order, update, node and edge fields")
    nodes = _array(message["nodes"], "VDA order nodes", minimum=1)
    edges = _array(message["edges"], "VDA order edges")
    normalized_nodes = []
    for node in nodes:
        if not isinstance(node, dict) or not {"nodeId", "sequenceId", "released"} <= set(node):
            raise ValueError("VDA order node requires ID, sequence and released flag")
        if type(node["released"]) is not bool:
            raise ValueError("VDA order node released flag must be boolean")
        normalized_nodes.append({"node_id": fp.text(node["nodeId"], "node id", 128),
                                 "sequence_id": fp.count(node["sequenceId"], 0, 2**31-1),
                                 "released": node["released"]})
    normalized_edges = []
    edge_ids: set[str] = set()
    for edge in edges:
        required_edge = {"edgeId", "sequenceId", "startNodeId", "endNodeId", "released"}
        if not isinstance(edge, dict) or not required_edge <= set(edge):
            raise ValueError("VDA order edge requires ID, sequence, endpoints and released flag")
        if type(edge["released"]) is not bool:
            raise ValueError("VDA order edge released flag must be boolean")
        edge_id = fp.text(edge["edgeId"], "edge id", 128)
        edge_ids.add(edge_id)
        normalized_edges.append({"edge_id": edge_id,
            "sequence_id": fp.count(edge["sequenceId"], 0, 2**31-1),
            "start": fp.text(edge["startNodeId"], "start node", 128),
            "end": fp.text(edge["endNodeId"], "end node", 128),
            "released": edge["released"]})
    return {**header, "order_id": fp.text(message["orderId"], "order id", 128),
            "order_update_id": fp.count(message["orderUpdateId"], 0, 2**31-1),
            "nodes": normalized_nodes, "edges": normalized_edges,
            "zones": _zones(message), "path_sharing": _path_sharing(message, edge_ids)}
