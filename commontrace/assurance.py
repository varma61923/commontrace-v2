"""Signed recall, measured provider usage, bounded action voting and replay sensitivity.

Replay scores are evaluator-dependent sensitivity evidence, never a lift proof.
Only a trusted local operator may supply an action evaluator or a replay judge.
"""
from __future__ import annotations

import math
import os
import random
import time
from collections.abc import Callable

from commontrace import _jsonl, memory_authority, origin, paths, policy


def _journal(root: str, name: str) -> str:
    return os.path.join(paths.memory_dir(root), name + ".jsonl")


def _operator():
    if memory_authority.WRITER.get()[1] != "operator":
        raise PermissionError("trusted operator evaluator required")


def _signed_append(root: str, name: str, record: dict, *, sources=None) -> dict:
    row = {**record, "origin": memory_authority.bind(root, record, sources=sources)}
    with _jsonl.locked(_journal(root, name)):
        _jsonl.append_row(_journal(root, name), row)
    return row


def record_recall(root: str, occasion: str, evidence: list[dict], context: str, scopes: list[str]) -> dict:
    sources = {r["record"].get("id"): r for r in _jsonl.read_rows(_journal(root, "origins"))
               if memory_authority.verify(root, r)}
    evidence = [{**row, "origin_record_sha256": policy.digest(sources[row["id"]]["record"])
                 if row["id"] in sources else None} for row in evidence]
    record = {"id": occasion, "kind": "recall-receipt", "evidence": evidence,
              "context": context, "scopes": scopes, "source_traces": [r["id"] for r in evidence]}
    with _jsonl.locked(_journal(root, "recall-receipts")):
        existing = next((r for r in _jsonl.read_rows(_journal(root, "recall-receipts"))
                         if r.get("id") == occasion), None)
        if existing:
            if not memory_authority.verify(root, existing.get("origin", {}), record):
                raise ValueError("occasion recall context is immutable")
            return existing
        return _signed_append(root, "recall-receipts", record, sources=record["source_traces"])


def recall(root: str, occasion: str) -> dict:
    row = next((r for r in _jsonl.read_rows(_journal(root, "recall-receipts")) if r.get("id") == occasion), None)
    if row is None:
        raise FileNotFoundError("no preceding recall receipt")
    if not memory_authority.verify(root, row.get("origin", {}),
                                                 {k: v for k, v in row.items() if k != "origin"}):
        raise ValueError("no authenticated recall receipt")
    return row


def usage(root: str, occasion: str, response, *, seconds: float, provider: str, prices: dict | None = None) -> dict:
    value = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    def field(*names):
        for name in names:
            count = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                return count
        return None
    inputs, outputs = field("input_tokens", "prompt_tokens"), field("output_tokens", "completion_tokens")
    prices = prices or {}
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in prices.values()):
        raise ValueError("prices must be finite nonnegative operator-supplied per-token amounts")
    cost = None
    if inputs is not None and outputs is not None and {"input", "output"} <= prices.keys():
        cost = inputs*prices["input"] + outputs*prices["output"]
    record = {"id": occasion, "kind": "provider-usage", "provider": provider,
              "input_tokens": inputs, "output_tokens": outputs, "seconds": seconds,
              "cost": cost, "prices": prices, "billing_eligible": False}
    return _signed_append(root, "provider-usage", record)


def failure(root: str, occasion: str, *, plan_valid=None, execution_valid=None,
            environment_valid=None, final: bool = True, outcome=None) -> dict:
    values = (plan_valid, execution_valid, environment_valid)
    if any(v is not None and not isinstance(v, bool) for v in values) or not isinstance(final, bool):
        raise ValueError("failure attribution requires explicit boolean observations or unknown")
    if outcome is not None and (not isinstance(outcome, (float, int)) or isinstance(outcome, bool)
                                or not math.isfinite(outcome) or not 0 <= outcome <= 1):
        raise ValueError("outcome must be finite in [0,1]")
    from commontrace.causal_policy import attribute_failure

    attribution = "environment" if environment_valid is False else attribute_failure(
        plan_valid=plan_valid, execution_valid=execution_valid)
    record = {"id": occasion, "kind": "outcome-attribution", "attribution": attribution,
              "plan_valid": plan_valid, "execution_valid": execution_valid,
              "environment_valid": environment_valid, "final": final, "outcome": outcome}
    return _signed_append(root, "outcome-attribution", record)


def forensics(root: str, occasion: str, evaluator: Callable[[list[dict]], float], *,
              seed: int = 0, probes: int = 20, seconds: float = 30) -> dict:
    _operator()
    if not 1 <= probes <= 100 or not math.isfinite(seconds) or not 0 < seconds <= 300:
        raise ValueError("invalid replay budget")
    receipt = recall(root, occasion)
    memories = receipt["evidence"]
    def score(items):
        value = evaluator(__import__("json").loads(origin._bytes({"items": items}))["items"])
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= 1):
            raise ValueError("replay evaluator must return a finite score in [0,1]")
        return float(value)
    started = time.monotonic()
    baseline = score(memories)
    results = []
    for index in random.Random(seed).sample(range(len(memories)), min(probes, len(memories))):
        if time.monotonic()-started >= seconds:
            break
        value = score([row for i, row in enumerate(memories) if i != index])
        results.append({"memory_id": memories[index]["id"], "full": baseline, "ablated": value,
                        "sensitivity": baseline-value, "source_sha256": policy.digest(memories[index])})
    record = {"id": occasion, "kind": "memory-incident-report", "receipt_sha256": policy.digest(receipt),
              "seed": seed, "probes": len(results), "budget_exhausted": len(results) < min(probes, len(memories)),
              "seconds": time.monotonic()-started, "causal_live_proof": False, "billing_eligible": False,
              "results": sorted(results, key=lambda r: -r["sensitivity"])}
    return _signed_append(root, "incident-reports", record, sources=receipt["source_traces"])



def _live_record(root: str, record_id: str):
    from commontrace import hierarchical, memory_control, ttl
    from commontrace.commands._traces import load_trace_instances

    fact = next((r for r in hierarchical.list_facts(root) if r.id == record_id), None)
    if fact:
        return memory_authority.fact_record(fact)
    trace = next((r for r in load_trace_instances(root) if r["id"] == record_id and ttl.trace_is_live(r)), None)
    if trace:
        return memory_authority.trace_record(trace)
    for kind in memory_control.KINDS:
        row = next((r for r in memory_control.records(root, kind) if r["id"] == record_id), None)
        if row:
            if kind in ("proposal", "compression"):
                from commontrace import compression, experience_skills

                selector = compression.active if kind == "compression" else experience_skills.active
                if not any(r["id"] == record_id for r in selector(root)):
                    return None
            if row["data"].get("status") not in (None, "active"):
                return None
            return {k: v for k, v in row.items() if k != "origin"}
    return None

def action_vote(root: str, occasion: str, action: dict, evaluator: Callable[[list[dict], dict], bool], *,
                seed: int = 0, trials: int = 20, seconds: float = 30, context: list[str] | None = None) -> dict:
    _operator()
    if not 2 <= trials <= 100 or not math.isfinite(seconds) or not 0 < seconds <= 300:
        raise ValueError("invalid action voting budget")
    if not isinstance(action, dict) or not isinstance(action.get("tool"), str) or not action["tool"]:
        raise ValueError("an action requires a tool")
    from commontrace import memory_control

    action = __import__("json").loads(origin._bytes(action))
    receipt = recall(root, occasion)
    if context is not None and receipt["scopes"] != context:
        raise PermissionError("action scope differs from authenticated recall")
    effective_context = receipt["scopes"]
    memory_control.check_action(root, action["tool"], tags=action.get("tags", []), context=effective_context)
    def validate_evidence():
        # Reconstruct current authority; a signed historical receipt cannot revive revoked sources.
        origins = {r["record"].get("id"): r for r in _jsonl.read_rows(_journal(root, "origins"))
                   if memory_authority.verify(root, r)}
        for item in receipt["evidence"]:
            signed = origins.get(item["id"])
            from commontrace import ttl

            current = _live_record(root, item["id"])
            if (current is None or not signed or current != signed["record"]
                    or item.get("origin_record_sha256") != policy.digest(current)
                    or not ttl.trace_is_live(signed["record"]) or not memory_authority.permits_record(
                    root, signed, signed["record"], action_class=action.get("class", action["tool"]))):
                raise PermissionError("action evidence has invalid, revoked or insufficient origin authority")
        memory_control.check_action(root, action["tool"], tags=action.get("tags", []), context=effective_context)

    validate_evidence()
    rng, started, votes = random.Random(seed), time.monotonic(), []
    for index in range(trials):
        if time.monotonic()-started >= seconds:
            break
        subset = ([] if index == 0 else receipt["evidence"] if index == 1 else
                  [r for r in receipt["evidence"] if rng.random() < .5])
        detached = __import__("json").loads(origin._bytes({"subset": subset, "action": action}))
        value = evaluator(detached["subset"], detached["action"])
        if not isinstance(value, bool):
            raise ValueError("action evaluator must return a boolean")
        votes.append({"included": [r["id"] for r in subset], "approve": value})
    validate_evidence()  # Revocation or policy changes during judging cannot authorize execution.
    record = {"id": occasion, "kind": "action-ablation-vote", "action_sha256": policy.digest(action),
              "receipt_sha256": policy.digest(receipt), "seed": seed, "trials": votes,
              "allowed": len(votes) == trials and time.monotonic()-started <= seconds
              and all(r["approve"] for r in votes),
              "seconds": time.monotonic()-started, "not_a_truth_guarantee": True}
    return _signed_append(root, "action-votes", record, sources=receipt["source_traces"])


def cost_report(root: str) -> dict:
    rows = []
    for row in _jsonl.read_rows(_journal(root, "provider-usage")):
        record = {k: v for k, v in row.items() if k != "origin"}
        if not memory_authority.verify(root, row.get("origin", {}), record):
            raise ValueError("invalid provider usage receipt")
        rows.append(record)
    measured = [r for r in rows if r["cost"] is not None]
    return {"calls": len(rows), "cost_known_calls": len(measured), "cost": sum(r["cost"] for r in measured),
            "latency_seconds": sum(r["seconds"] for r in rows), "billing_eligible": False,
            "usage": rows, "causal_policy": policy.evaluate(root, {"kind": "exploration-delivery"})}
