"""Small, bounded-time STL monitor for memory-linked robot action checks.

The monitor computes discrete-time robustness over caller-supplied telemetry. It
is a runtime guardrail and evidence record, not a certified safety controller.
"""
from __future__ import annotations

import math
import re
import time
from bisect import bisect_left, bisect_right
from typing import Any, cast

from commontrace import _jsonl
from commontrace import frontier_protocol as fp

JOURNAL = "robot-shield.jsonl"
MAX_MONITOR_WORK = 1_000_000
MAX_FORMULA_NODES = 256


class _MonitorBudgetExceeded(Exception):
    pass


class _IncompleteTelemetry(Exception):
    pass


def _formula(node: Any, depth: int = 0, work: list[int] | None = None) -> dict[str, Any]:
    work = [0] if work is None else work
    work[0] += 1
    if work[0] > MAX_FORMULA_NODES:
        raise ValueError("STL formula exceeds the node bound")
    if depth > 16 or not isinstance(node, dict):
        raise ValueError("STL formula must be a bounded-depth object")
    op = node.get("op")
    if not isinstance(op, str):
        raise ValueError("STL operator must be a string")
    if op == "predicate":
        if set(node) != {"op", "signal", "cmp", "value"}:
            raise ValueError("predicate requires op, signal, cmp and value")
        signal = fp.text(node["signal"], "signal", 96)
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]*", signal):
            raise ValueError("invalid telemetry signal")
        if not isinstance(node["cmp"], str) or node["cmp"] not in {">=", ">", "<=", "<"}:
            raise ValueError("unsupported STL comparison")
        value = fp.number(node["value"], -1e12, 1e12)
        return {"op": op, "signal": signal, "cmp": node["cmp"], "value": value}
    if op in {"always", "eventually"}:
        if set(node) != {"op", "from", "to", "arg"}:
            raise ValueError("temporal operator requires op, from, to and arg")
        start, end = fp.number(node["from"], 0, 1e6), fp.number(node["to"], 0, 1e6)
        if end < start:
            raise ValueError("STL interval end must be >= start")
        return {"op": op, "from": start, "to": end, "arg": _formula(node["arg"], depth + 1, work)}
    if op == "until":
        if set(node) != {"op", "from", "to", "left", "right"}:
            raise ValueError("until requires op, from, to, left and right")
        start, end = fp.number(node["from"], 0, 1e6), fp.number(node["to"], 0, 1e6)
        if end < start:
            raise ValueError("STL interval end must be >= start")
        return {"op": op, "from": start, "to": end,
                "left": _formula(node["left"], depth + 1, work), "right": _formula(node["right"], depth + 1, work)}
    raise ValueError("STL operator must be predicate, always, eventually or until")


def _robust(node: dict[str, Any], trace: list[dict[str, Any]], times: list[float], at: float,
            index: dict[float, int], work: list[int]) -> float:
    work[0] += 1
    if work[0] > MAX_MONITOR_WORK:
        raise _MonitorBudgetExceeded
    op = node["op"]
    if op == "predicate":
        row = trace[index[at]]
        if node["signal"] not in row:
            raise ValueError(f"telemetry is missing signal {node['signal']!r} at t={at}")
        value = fp.number(row[node["signal"]], -1e12, 1e12)
        threshold = cast(float, node["value"])
        cmp = node["cmp"]
        if cmp == ">=":
            return value - threshold
        if cmp == ">":
            return value - math.nextafter(threshold, math.inf)
        if cmp == "<=":
            return threshold - value
        if cmp == "<":
            return math.nextafter(threshold, -math.inf) - value
    if at + node["to"] > times[-1]:
        raise _IncompleteTelemetry
    lo = bisect_left(times, at + node["from"])
    hi = bisect_right(times, at + node["to"])
    selected = times[lo:hi]
    if not selected:
        raise ValueError("STL interval contains no telemetry samples")
    if op == "always":
        return min(_robust(node["arg"], trace, times, t, index, work) for t in selected)
    if op == "eventually":
        return max(_robust(node["arg"], trace, times, t, index, work) for t in selected)
    # Discrete bounded until: right must become true in the interval, while left
    # remains true at every earlier sample from the current point.
    best, prefix = -math.inf, math.inf
    for position in range(bisect_left(times, at), hi):
        t = times[position]
        if t >= at + node["from"]:
            best = max(best, min(prefix, _robust(node["right"], trace, times, t, index, work)))
        if position + 1 < hi:
            prefix = min(prefix, _robust(node["left"], trace, times, t, index, work))
    return best


def register(root: str, spec_id: str, *, formula: dict[str, Any], memory_id: str,
             embodiment: str, description: str) -> dict[str, Any]:
    spec_id = fp.text(spec_id, "spec id", 96)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,95}", spec_id):
        raise ValueError("spec id must be a lowercase identifier")
    normalized = _formula(formula)
    body = {"spec_id": spec_id, "formula": normalized, "memory_id": fp.text(memory_id, "memory id", 256),
            "embodiment": fp.text(embodiment, "embodiment", 96),
            "description": fp.text(description, "description", 1024), "at": fp.now()}
    with _jsonl.locked(fp.filename(root, JOURNAL)):
        if any(row.get("type") == "spec" and row["spec_id"] == spec_id for row in fp.journal(root, JOURNAL)):
            raise ValueError("STL spec identifiers are immutable")
        return cast(dict[str, Any], fp.append(root, JOURNAL, {"type": "spec", **body})["record"])


def check(root: str, spec_id: str, *, action: dict[str, Any], trace: list[dict[str, Any]], source_memory_ids: list[str],
          recovery_memory_ids: list[str] | None = None, recovery_context: dict[str, str] | None = None,
          failure_type: str | None = None) -> dict[str, Any]:
    start_ns = time.perf_counter_ns()
    rows = [row for row in fp.journal(root, JOURNAL) if row.get("type") == "spec" and row["spec_id"] == spec_id]
    if not rows:
        raise KeyError("unknown STL spec")
    if not isinstance(trace, list) or not trace or len(trace) > 100_000:
        raise ValueError("telemetry trace must contain 1..100000 samples")
    fp.wire(trace)
    if not isinstance(source_memory_ids, list) or len(source_memory_ids) > 1024:
        raise ValueError("shield sources must be an array of at most 1024 memory IDs")
    if recovery_memory_ids is not None and (
            not isinstance(recovery_memory_ids, list) or len(recovery_memory_ids) > 1024):
        raise ValueError("shield recoveries must be an array of at most 1024 memory IDs")
    clean: list[dict[str, float]] = []
    times: list[float] = []
    for row in trace:
        if not isinstance(row, dict) or "t" not in row or not isinstance(row["t"], (int, float)):
            raise ValueError("each telemetry sample must have a finite time t")
        t = fp.number(row["t"], 0, 1e9)
        if times and t <= times[-1]:
            raise ValueError("telemetry times must be strictly increasing")
        values = {str(k): fp.number(v, -1e12, 1e12) for k, v in row.items() if k != "t"}
        clean.append(values)
        times.append(t)
    spec = rows[-1]
    if not isinstance(action, dict) or not action:
        raise ValueError("shield requires the proposed robot action")
    fp.wire(action)
    sources = sorted({fp.text(v, "source memory id", 256) for v in source_memory_ids})
    index = {t: i for i, t in enumerate(times)}
    work = [0]
    if spec["memory_id"] not in sources:
        verdict, robustness = "memory-not-recalled", None
    else:
        try:
            robustness = _robust(spec["formula"], clean, times, times[0], index, work)
            verdict = "safe" if robustness >= 0 else "violated"
        except _MonitorBudgetExceeded:
            robustness, verdict = None, "monitor-budget-exceeded"
        except _IncompleteTelemetry:
            robustness, verdict = None, "incomplete-telemetry"
    fallback: list[dict[str, Any]] = []
    if recovery_memory_ids and (recovery_context is None or not failure_type):
        raise ValueError("selected recovery memories require failure type and scoped registry context")
    if verdict == "violated" and recovery_context is not None and failure_type:
        from commontrace import recovery as recovery_registry

        candidates = recovery_registry.recall(root, {"failure_type": fp.text(failure_type, "failure type", 128)},
                                              recovery_context)["recoveries"]
        wanted = set(recovery_memory_ids or [])
        fallback = [row for row in candidates if row["hazard"] and (not wanted or row["id"] in wanted)]
    recovery = [row["id"] for row in fallback]
    elapsed = (time.perf_counter_ns() - start_ns) / 1_000_000
    result = {"spec_id": spec_id, "verdict": verdict, "robustness": robustness,
              "proposed_action_digest": fp.digest(action),
              "source_memory_ids": sources, "recovery_memory_ids": recovery,
              "recovery_fallback": fallback,
              "action": "continue" if verdict == "safe" else "hold-and-use-recovery" if recovery else "hold",
              "monitor_work": work[0],
              "latency_ms": elapsed, "certified_safety_function": False}
    # Preserve the legacy signals-only commitment and add a separate binding
    # for sample times, which determine temporal semantics.
    trace_digest = fp.digest(clean)
    telemetry_digest = fp.digest({"times": times, "signals": clean})
    result["telemetry_digest"] = telemetry_digest
    lineage_id = "shield_verdict_" + fp.digest(result)[:32]
    result["lineage_id"] = lineage_id
    fp.append(root, JOURNAL, {"type": "verdict", **result, "trace_digest": trace_digest, "at": fp.now()})
    from commontrace import lineage

    lineage_record = {"id": lineage_id, "memory_id": spec["memory_id"],
                      "verdict": verdict, "proposed_action_digest": result["proposed_action_digest"],
                      "trace_digest": trace_digest, "telemetry_digest": telemetry_digest, "scopes": []}
    lineage.attest(root, lineage_record, fp.sign(root, lineage_record), inputs=[spec["memory_id"]],
                   operation="robot-shield-verdict")
    return result


def list_specs(root: str) -> list[dict[str, Any]]:
    return [row for row in fp.journal(root, JOURNAL) if row.get("type") == "spec"]
