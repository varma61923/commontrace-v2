"""Vendor-neutral bitemporal site state, scene layers and estimated congestion rules."""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, cast

from commontrace import frontier_protocol as fp

JOURNAL = "robot-site-memory.jsonl"


def ingest_state(root: str, normalized: dict[str, Any], *, site: str, robot: str, vendor: str,
                 kind: str = "state", scopes: list[str] | None = None) -> dict[str, Any]:
    if kind not in {"state", "zone", "path"}:
        raise ValueError("unknown site-memory event kind")
    site, robot, vendor = (fp.text(value, name, 128) for value, name in
                           ((site, "site"), (robot, "robot"), (vendor, "vendor")))
    event_time = fp.moment(normalized["timestamp"])
    body = {"id": "site-" + fp.digest([site, robot, vendor, normalized])[:32], "site": site,
            "robot": robot, "vendor": vendor, "kind": kind, "valid_from": event_time,
            "recorded_at": fp.now(), "scopes": sorted(set(scopes or [])),
            "state": normalized}
    return cast(dict[str, Any], fp.append(root, JOURNAL, body)["record"])


def ingest_vda_state(root: str, message: dict[str, Any], *, site: str, robot: str,
                     scopes: list[str] | None = None) -> dict[str, Any]:
    from commontrace.robotics.vda5050 import validate_state

    return ingest_state(root, validate_state(message), site=site, robot=robot,
                        vendor="vda5050-3.0.0", scopes=scopes)


def ingest_open_rmf(root: str, adapter: Any, message: Any, *, site: str, robot: str,
                     scopes: list[str] | None = None) -> dict[str, Any]:
    """Map Open-RMF data via an injected adapter; the core does not import RMF."""
    if not callable(getattr(adapter, "to_commontrace_state", None)):
        raise TypeError("Open-RMF adapter must implement to_commontrace_state")
    normalized = adapter.to_commontrace_state(message)
    fp.wire(normalized)
    if not isinstance(normalized, dict) or not {"timestamp", "nodes", "edges", "zones"} <= set(normalized):
        raise ValueError("Open-RMF adapter returned an invalid normalized site state")
    return ingest_state(root, normalized, site=site, robot=robot, vendor="open-rmf-adapter", scopes=scopes)


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
