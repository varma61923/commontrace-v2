"""Vendor-neutral bitemporal site state, scene layers and estimated congestion rules."""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, Protocol, cast

from commontrace import frontier_protocol as fp

JOURNAL = "robot-site-memory.jsonl"


class OpenRMFAdapter(Protocol):
    """Deployment adapter that converts an RMF message into the bounded site schema."""

    def to_commontrace_state(self, message: Any) -> dict[str, Any]: ...


def _validated_state(value: Any) -> dict[str, Any]:
    """Copy and validate the adapter-neutral subset before persisting site data."""
    if not isinstance(value, dict):
        raise ValueError("site adapter must return an object")
    copied = fp.loads(fp.wire(value))
    if not isinstance(copied, dict) or not {"timestamp", "nodes", "edges", "zones"} <= set(copied):
        raise ValueError("site state requires timestamp, nodes, edges and zones")
    fp.moment(copied["timestamp"])
    nodes, edges, zones = copied["nodes"], copied["edges"], copied["zones"]
    if not isinstance(nodes, list) or not isinstance(edges, list) or not isinstance(zones, list):
        raise ValueError("site nodes, edges and zones must be lists")
    fp.count(len(nodes), 0, 4096, "site nodes")
    fp.count(len(edges), 0, 4096, "site edges")
    fp.count(len(zones), 0, 512, "site zones")
    path_sharing = copied.get("path_sharing")
    if path_sharing is not None and (not isinstance(path_sharing, list) or len(path_sharing) > 4096):
        raise ValueError("site path sharing must be a list of at most 4096 edge IDs")
    shared_ids = [fp.text(edge_id, "shared edge id", 128) for edge_id in (path_sharing or [])]
    edge_ids = set()
    for node in nodes:
        if not isinstance(node, dict) or not ("node_id" in node or "id" in node):
            raise ValueError("site node requires node_id or id")
        fp.text(cast(str, node.get("node_id", node.get("id"))), "site node id", 128)
    for edge in edges:
        if not isinstance(edge, dict) or not ("edge_id" in edge or "id" in edge):
            raise ValueError("site edge requires edge_id or id")
        edge_ids.add(fp.text(cast(str, edge.get("edge_id", edge.get("id"))), "site edge id", 128))
    for zone in zones:
        fp.text(zone, "site zone", 128)
    if not set(shared_ids) <= edge_ids:
        raise ValueError("site path sharing must reference an edge in the same state")
    return copied


def ingest_state(root: str, normalized: dict[str, Any], *, site: str, robot: str, vendor: str,
                 kind: str = "state", scopes: list[str] | None = None) -> dict[str, Any]:
    if kind not in {"state", "zone", "path"}:
        raise ValueError("unknown site-memory event kind")
    normalized = _validated_state(normalized)
    site, robot, vendor = (fp.text(value, name, 128) for value, name in
                           ((site, "site"), (robot, "robot"), (vendor, "vendor")))
    event_time = fp.moment(normalized["timestamp"])
    if scopes is not None and (not isinstance(scopes, list) or len(scopes) > 128):
        raise ValueError("site scopes must be a list of at most 128 labels")
    normalized_scopes = sorted({fp.text(scope, "site scope", 128) for scope in (scopes or [])})
    body = {"id": "site-" + fp.digest([site, robot, vendor, normalized])[:32], "site": site,
            "robot": robot, "vendor": vendor, "kind": kind, "valid_from": event_time,
            "recorded_at": fp.now(), "scopes": normalized_scopes,
            "state": normalized}
    return cast(dict[str, Any], fp.append(root, JOURNAL, body)["record"])


def ingest_vda_state(root: str, message: dict[str, Any], *, site: str, robot: str,
                     scopes: list[str] | None = None, kind: str = "state") -> dict[str, Any]:
    from commontrace.robotics.vda5050 import validate_state

    return ingest_state(root, validate_state(message), site=site, robot=robot,
                        vendor="vda5050-3.0.0", kind=kind, scopes=scopes)


def ingest_vda_order(root: str, message: dict[str, Any], *, site: str, robot: str,
                     scopes: list[str] | None = None) -> dict[str, Any]:
    """Persist a bounded VDA order route as a path event for fleet scene sharing."""
    from commontrace.robotics.vda5050 import validate_order

    return ingest_state(root, validate_order(message), site=site, robot=robot,
                        vendor="vda5050-3.0.0", kind="path", scopes=scopes)


def ingest_vda_zone(root: str, message: dict[str, Any], *, site: str, robot: str,
                    scopes: list[str] | None = None) -> dict[str, Any]:
    """Persist a bounded zone-only VDA adapter event without inventing node data."""
    from commontrace.robotics.vda5050 import validate_zone

    zone_state = validate_zone(message)
    normalized = {**zone_state, "nodes": [], "edges": []}
    return ingest_state(root, normalized, site=site, robot=robot,
                        vendor="vda5050-3.0.0", kind="zone", scopes=scopes)


def ingest_open_rmf(root: str, adapter: OpenRMFAdapter, message: Any, *, site: str, robot: str,
                     scopes: list[str] | None = None) -> dict[str, Any]:
    """Map Open-RMF data via an injected adapter; the core does not import RMF."""
    if not callable(getattr(adapter, "to_commontrace_state", None)):
        raise TypeError("Open-RMF adapter must implement to_commontrace_state")
    return ingest_state(root, adapter.to_commontrace_state(message), site=site, robot=robot,
                        vendor="open-rmf-adapter", scopes=scopes)


def scene_graph(root: str, *, site: str, context: Sequence[str] = ()) -> dict[str, Any]:
    rows = [row for row in fp.journal(root, JOURNAL) if row["site"] == site
            and set(row["scopes"]) <= set(context)]
    layers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        state = row["state"]
        for node in state.get("nodes", []):
            identity = node.get("node_id", node.get("id"))
            if identity:
                layers[identity].append({"robot": row["robot"], "vendor": row["vendor"],
                    "position": node.get("position"), "valid_from": row["valid_from"],
                    "recorded_at": row["recorded_at"]})
    return {"site": site, "layers": {key: sorted(values, key=lambda value: (value["valid_from"], value["robot"]))
                                      for key, values in sorted(layers.items())},
            "kind": "layer-preserving multi-robot scene merge; conflicting sensor frames remain separate"}


def rules(root: str, *, site: str, observations: list[dict[str, Any]],
          minimum_observations: int = 10) -> list[dict[str, Any]]:
    """Estimate zone congestion by local hour; suggestions are review-only."""
    fp.count(len(observations), 0, 100_000)
    fp.count(minimum_observations, 1, 10_000)
    grouped: dict[tuple[str, int], list[bool]] = defaultdict(list)
    for row in observations:
        timestamp = fp.moment(row["timestamp"])
        zone = fp.text(row["zone"], "zone", 128)
        if type(row["congested"]) is not bool:
            raise ValueError("congestion labels must be boolean observations")
        grouped[(zone, int(timestamp[11:13]))].append(row["congested"])
    result = []
    for (zone, hour), values in sorted(grouped.items()):
        if len(values) < minimum_observations:
            continue
        n, positives = len(values), sum(values)
        p = positives / n
        radius = 1.96 * math.sqrt(max(0, p * (1 - p) / n))
        result.append({"site": site, "zone": zone, "hour": hour, "observations": n,
                       "congested_rate": p, "interval": [max(0, p-radius), min(1, p+radius)],
                       "suggestion": "review whether this zone should be avoided during this hour",
                       "kind": "normal-approximation estimate; not a forecast or routing command"})
    return result


def suggest(root: str, *, site: str, ruleset: list[dict[str, Any]]) -> dict[str, Any]:
    body = {"site": fp.text(site, "site", 128), "rules": fp.wire(ruleset).decode(),
            "status": "review-required", "created_at": fp.now()}
    signed = fp.append(root, "robot-site-suggestions.jsonl", body)
    return cast(dict[str, Any], signed["record"])
