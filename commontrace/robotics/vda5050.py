"""Locally authored VDA 5050 3.0 interoperability subset for state and orders.

This validates only fields consumed by site memory; it is not a full standard
schema or conformance implementation.
"""
from __future__ import annotations

from typing import Any

from commontrace import frontier_protocol as fp

VERSION = "3.0.0"


def _header(message: dict[str, Any]) -> dict[str, Any]:
    required = {"headerId", "timestamp", "version", "manufacturer", "serialNumber"}
    if not required <= set(message) or message["version"] != VERSION:
        raise ValueError("VDA 5050 interoperability subset requires version 3.0.0 header fields")
    fp.count(message["headerId"], 0, 2**63 - 1, "VDA header id")
    fp.moment(message["timestamp"])
    return {"header_id": message["headerId"], "timestamp": message["timestamp"],
            "manufacturer": fp.text(message["manufacturer"], "manufacturer", 128),
            "serial_number": fp.text(message["serialNumber"], "serial number", 128)}


def validate_state(message: dict[str, Any]) -> dict[str, Any]:
    header = _header(message)
    if "orderId" not in message or "orderUpdateId" not in message:
        raise ValueError("VDA state subset requires order ID and update ID")
    nodes, edges = message.get("nodeStates", []), message.get("edgeStates", [])
    fp.count(len(nodes), 0, 4096, "VDA nodes")
    fp.count(len(edges), 0, 4096, "VDA edges")
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
    for edge in edges:
        if not isinstance(edge, dict) or not {"edgeId", "sequenceId", "startNodeId", "endNodeId"} <= set(edge):
            raise ValueError("VDA edge state requires edge ID, sequence and endpoints")
        released = edge.get("released", False)
        if type(released) is not bool:
            raise ValueError("VDA released flag must be boolean")
        normalized_edges.append({"edge_id": fp.text(edge["edgeId"], "edge id", 128),
            "sequence_id": fp.count(edge["sequenceId"], 0, 2**31-1),
            "start": fp.text(edge["startNodeId"], "start node", 128),
            "end": fp.text(edge["endNodeId"], "end node", 128),
            "released": released})
    zones = message.get("zoneIds", [])
    fp.count(len(zones), 0, 512, "VDA zones")
    return {**header, "order_id": fp.text(message["orderId"], "order id", 128),
            "order_update_id": fp.count(message["orderUpdateId"], 0, 2**31-1),
            "nodes": normalized_nodes, "edges": normalized_edges,
            "zones": sorted(set(fp.text(value, "zone id", 128) for value in zones)),
            "path_sharing": message.get("pathSharing", [])}


def validate_order(message: dict[str, Any]) -> dict[str, Any]:
    header = _header(message)
    if not {"orderId", "orderUpdateId", "nodes", "edges"} <= set(message):
        raise ValueError("VDA order subset requires order, update, node and edge fields")
    nodes, edges = message["nodes"], message["edges"]
    fp.count(len(nodes), 1, 4096)
    fp.count(len(edges), 0, 4096)
    normalized_nodes = []
    for node in nodes:
        if not isinstance(node, dict) or not {"nodeId", "sequenceId", "released"} <= set(node):
            raise ValueError("VDA order node requires ID, sequence and released flag")
        normalized_nodes.append({"node_id": fp.text(node["nodeId"], "node id", 128),
                                 "sequence_id": fp.count(node["sequenceId"], 0, 2**31-1),
                                 "released": type(node["released"]) is bool and node["released"]})
    normalized_edges = []
    for edge in edges:
        required_edge = {"edgeId", "sequenceId", "startNodeId", "endNodeId", "released"}
        if not isinstance(edge, dict) or not required_edge <= set(edge):
            raise ValueError("VDA order edge requires ID, sequence, endpoints and released flag")
        normalized_edges.append({"edge_id": fp.text(edge["edgeId"], "edge id", 128),
            "sequence_id": fp.count(edge["sequenceId"], 0, 2**31-1),
            "start": fp.text(edge["startNodeId"], "start node", 128),
            "end": fp.text(edge["endNodeId"], "end node", 128),
            "released": type(edge["released"]) is bool and edge["released"]})
    return {**header, "order_id": fp.text(message["orderId"], "order id", 128),
            "order_update_id": fp.count(message["orderUpdateId"], 0, 2**31-1),
            "nodes": normalized_nodes, "edges": normalized_edges}
