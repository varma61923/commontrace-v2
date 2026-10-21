"""Markdown compression ladder with preregistered adjacent and raw controls.

This is a separate fixed-horizon randomized experiment. It never changes the
Protocol 13.1 holdout or makes its experimental outputs billable.
"""
from __future__ import annotations

import hashlib
import math
import os
import uuid
from typing import Any, Protocol, Sequence

from unimem import _jsonl, approval, memory_authority, memory_control, paths, policy

LEVELS = ("trace", "episode", "observation", "lesson", "skill", "directive")


class KVReuse(Protocol):
    """Optional self-hosted KV adapter; implementations live in provider extras."""

    def prepare(self, version: str, text: str) -> str: ...
    def invalidate(self, version: str) -> None: ...


def cache_prefix(items: Sequence[dict[str, Any]], *, pricing: dict[str, float] | None = None,
                 max_churn: int = 2, minimum_value: float = .01) -> dict[str, Any]:
    """Deterministic approved, positive-value prefix before a provider breakpoint.

    Cache prices and latency rates are supplied by the deployment, never baked
    into the package. Dollar/latency numbers are estimates, not measured savings.
    Callers must pass already-governed candidates; this function does not retrieve.
    """
    from unimem import frontier_protocol as fp

    fp.count(len(items), 0, 256)
    fp.count(max_churn, 0, 1000)
    fp.number(minimum_value, 0, 1)
    accepted = []
    for item in items:
        fp.text(item["id"])
        fp.text(item["text"], "prefix memory", 64_000)
        value = fp.number(item.get("value", 0), -1, 1)
        churn = fp.count(item.get("churn", 0), 0, 100_000)
        if item.get("approved") is True and value >= minimum_value and churn <= max_churn:
            accepted.append({"id": item["id"], "text": item["text"]})
    accepted.sort(key=lambda item: item["id"])
    content = "\n\n".join("[" + item["id"] + "]\n" + item["text"].strip() for item in accepted)
    version = fp.digest(accepted)
    tokens = (len(content) + 3) // 4
    bill = {"estimated_tokens": tokens, "tokenizer": "four-character estimate", "savings_usd": None,
            "latency_savings_ms": None, "kind": "configuration-based estimate"}
    if pricing is not None:
        for name in ("uncached_per_million", "cached_per_million", "uncached_ms_per_token", "cached_ms_per_token"):
            fp.number(pricing[name], 0, 1e6, name)
        bill["savings_usd"] = tokens * (pricing["uncached_per_million"] - pricing["cached_per_million"]) / 1e6
        bill["latency_savings_ms"] = tokens * (pricing["uncached_ms_per_token"] - pricing["cached_ms_per_token"])
    return {"version": version, "text": content, "ids": [item["id"] for item in accepted],
            "placement": "before-provider-cache-breakpoint", "context_bill": bill}


def proven_prefix(root: str, admitted: Sequence[dict[str, Any]], *, pricing: dict[str, float] | None = None) -> dict:
    """Build a prefix only from freshly approved, evidence-positive delivered lessons."""
    from unimem import evidence, frontmatter, lesson_admission, lesson_io

    positive = evidence.graduated(root)
    from unimem import frontier_protocol as fp

    history = fp.rows(lesson_io.revisions_log_path(root))
    rows = []
    for item in admitted:
        identity = item.get("slug", "")
        path = lesson_io.lesson_path(root, identity)
        if identity not in positive or path is None:
            continue
        fm, body = frontmatter.read(path)
        if (body == item.get("body") and fm.get("approval_receipt")
                and lesson_admission.eligible(root, path, fm, body)):
            changes = sum(lesson_io.canonical_slug(row["lesson"]) == identity for row in history)
            rows.append({"id": identity, "text": body, "value": positive[identity]["effect"],
                         "approved": True, "churn": changes})
    prefix = cache_prefix(rows, pricing=pricing)
    from unimem import frontier_protocol as fp

    prefix["namespace"] = fp.digest({"store": os.path.realpath(root)})
    prefix["version"] = fp.digest({"namespace": prefix["namespace"], "content": prefix["version"]})
    if prefix["ids"]:
        from unimem import lineage

        record = {"id": "prefix-" + prefix["version"], "version": prefix["version"], "text": prefix["text"]}
        if record["id"] not in lineage.records(root):
            lineage.attest(root, record, fp.sign(root, record), inputs=prefix["ids"], operation="cache-prefix")
    return prefix


def _file(root):
    return os.path.join(paths.memory_dir(root), "compression-trials.jsonl")


def _events(root):
    events = _jsonl.read_rows(_file(root))
    for row in events:
        if not memory_authority.verify(root, row.get("origin", {}), {k: v for k, v in row.items() if k != "origin"}):
            raise PermissionError("compression trial receipt invalid")
    return events


def _append(root, record):
    _jsonl.append_row(_file(root), {**record, "origin": memory_authority.bind(root, record)})


def _source(root, source_id):
    from unimem import ttl
    from unimem.commands._traces import load_trace_instances

    trace = next((r for r in load_trace_instances(root) if r["id"] == source_id), None)
    if trace is None or not ttl.trace_is_live(trace):
        raise ValueError("compression requires live source traces")
    record = memory_authority.trace_record(trace)
    receipt = trace.get("extensions", {}).get("profile", {}).get("origin", {})
    if not memory_authority.permits_record(root, receipt, record) or not receipt:
        raise PermissionError("compression requires authenticated source traces")
    return {"id": source_id, "digest": policy.digest(record),
            "text": trace.get("context_text", "")+"\n"+trace.get("solution_text", ""),
            "scopes": trace.get("scopes", [])}


def propose(root: str, text: str, *, level: str, sources: list[str], parent: str = "", actor: str,
            applies_when: str, do_not_apply_when: str, deny_tools: list[str] | None = None) -> dict:
    from unimem import safety

    safety.assert_mutable(root, sources)
    if level not in LEVELS[1:] or not sources or not actor or not applies_when or not do_not_apply_when:
        raise ValueError("level, live sources, author and applicability bounds required")
    raw = [_source(root, sid) for sid in sorted(set(sources))]
    parent_row = None
    if LEVELS.index(level) > 1:
        parent_row = next((r for r in active(root) if r["id"] == parent), None)
        if parent_row is None or parent_row["data"]["level"] != LEVELS[LEVELS.index(level)-1]:
            raise ValueError("promotion must compare against an active adjacent-level parent")
        if set(parent_row["data"]["sources"]) != set(sources):
            raise ValueError("adjacent parent must retain identical raw evidence")
    labels = sorted({scope for row in raw for scope in row["scopes"]})
    for tool in deny_tools or []:
        if not isinstance(tool, str) or not tool or len(tool) > 128:
            raise ValueError("invalid deny tool identifier")
    return memory_control.put(root, "compression", text, actor=actor, labels=labels, data={
        "level": level, "parent": parent, "status": "review", "sources": sorted(set(sources)),
        "source_digests": {r["id"]: r["digest"] for r in raw}, "author": actor,
        "parent_sha256": policy.digest(parent_row) if parent_row else None,
        "applies_when": applies_when, "do_not_apply_when": do_not_apply_when,
        "deny_tools": deny_tools or []})


def active(root: str, *, context: list[str] | None = None) -> list[dict]:
    rows = memory_control.records(root, "compression")
    candidates = {r["id"]: r for r in rows if r["data"].get("status") == "active"}
    admissions = [r for r in _events(root) if r["event"] == "admit"]
    valid = {}
    for row in sorted(candidates.values(), key=lambda r: LEVELS.index(r["data"]["level"])):
        data = row["data"]
        admission = next((r for r in admissions if r["proposal_id"] == row["id"]
                          and r["admitted_sha256"] == policy.digest(row)), None)
        if (admission is None or admission["actor"] == data["author"]
                or evaluate(root, admission["experiment_id"])["verdict"] != "HELPS"):
            continue
        experiments = [r for r in _events(root) if r["event"] == "register"
                       and r["proposal_id"] == row["id"] and r["proposal_sha256"] == policy.digest(row)]
        if any(evaluate(root, e["id"])["verdict"] == "HURTS" for e in experiments):
            continue
        if memory_authority.lineage_blocked(root, row["id"]):
            continue
        try:
            if any(_source(root, sid)["digest"] != digest for sid, digest in data["source_digests"].items()):
                continue
        except (ValueError, PermissionError):
            continue
        parent = data["parent"]
        if parent and (parent not in valid or policy.digest(valid[parent]) != data["parent_sha256"]):
            continue
        valid[row["id"]] = row
    return [r for r in valid.values() if context is None or memory_control.matches(
        r["scopes"], context, governed=r["data"]["level"] != "directive")]


def register(root: str, proposal_id: str, *, trials: int = 300, seed: str | None = None) -> dict:
    if not memory_authority.is_operator():
        raise PermissionError("compression preregistration requires an operator")
    if isinstance(trials, bool) or not isinstance(trials, int) or not 90 <= trials <= 100000:
        raise ValueError("fixed horizon must be 90-100000 occasions")
    row = next((r for r in memory_control.records(root, "compression") if r["id"] == proposal_id), None)
    if row is None:
        raise ValueError("unknown compression proposal")
    raw = [_source(root, sid) for sid in row["data"]["sources"]]
    parent = next((r for r in active(root) if r["id"] == row["data"]["parent"]), None)
    if row["data"]["parent"] and not parent:
        raise ValueError("parent is no longer active")
    record = {"id": uuid.uuid4().hex, "event": "register", "proposal_id": proposal_id,
              "proposal_sha256": policy.digest(row), "trials": trials, "seed": seed or uuid.uuid4().hex,
              "contexts": {"candidate": row["text"], "parent": parent["text"] if parent else
                           "\n".join(r["text"] for r in raw), "raw": "\n".join(r["text"] for r in raw)},
              "alpha": .05, "source_sha256": policy.digest(raw)}
    with _jsonl.locked(_file(root)):
        _append(root, record)
    return record


def next_trial(root: str, experiment_id: str) -> dict:
    with _jsonl.locked(_file(root)):
        events = _events(root)
        experiment = next((r for r in events
                           if r["id"] == experiment_id and r["event"] == "register"), None)
        if experiment is None:
            raise ValueError("unknown compression experiment")
        index = sum(r["event"] == "assign" and r["experiment_id"] == experiment_id for r in events)
        if index >= experiment["trials"]:
            raise ValueError("preregistered trial budget exhausted")
        # Uniform pseudo-random assignment from the committed seed and index.
        arm_index = int.from_bytes(
            hashlib.sha256((experiment["seed"]+"\0"+str(index)).encode()).digest(), "big") % 3
        arm = ("candidate", "parent", "raw")[arm_index]
        record = {"id": uuid.uuid4().hex, "event": "assign", "experiment_id": experiment_id,
                  "index": index, "arm": arm, "probability": 1/3,
                  "context_sha256": policy.digest({"context": experiment["contexts"][arm]})}
        _append(root, record)
        return {**record, "context": experiment["contexts"][arm]}


def outcome(root: str, trial_id: str, value: float):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 1):
        raise ValueError("trial outcome must be finite in [0,1]")
    with _jsonl.locked(_file(root)):
        events = _events(root)
        if not any(r["id"] == trial_id and r["event"] == "assign" for r in events):
            raise ValueError("unknown trial")
        prior = next((r for r in events if r["id"] == trial_id and r["event"] == "outcome"), None)
        if prior:
            if prior["value"] != value:
                raise ValueError("trial outcome is immutable")
            return
        _append(root, {"id": trial_id, "event": "outcome", "value": value})


def evaluate(root: str, experiment_id: str) -> dict:
    events = _events(root)
    experiment = next((r for r in events if r["id"] == experiment_id and r["event"] == "register"), None)
    if experiment is None:
        raise ValueError("unknown compression experiment")
    assigned = [r for r in events if r["event"] == "assign" and r["experiment_id"] == experiment_id]
    outcomes = {r["id"]: r["value"] for r in events if r["event"] == "outcome"}
    report = {"experiment_id": experiment_id, "billing_eligible": False, "verdict": "UNIDENTIFIED",
              "proposal_sha256": experiment["proposal_sha256"], "log_sha256": policy.digest(events)}
    if len(assigned) != experiment["trials"] or any(r["id"] not in outcomes for r in assigned):
        return {**report, "reason": "complete preregistered fixed horizon required"}
    groups = {arm: [outcomes[r["id"]] for r in assigned if r["arm"] == arm]
              for arm in ("candidate", "parent", "raw")}
    if any(len(v) < 30 for v in groups.values()):
        return {**report, "reason": "insufficient randomized arm support"}
    means = {arm: sum(v)/len(v) for arm, v in groups.items()}
    comparisons = {}
    for arm in ("parent", "raw"):
        radius = sum(math.sqrt(math.log(8/experiment["alpha"])/(2*len(groups[a])))
                     for a in ("candidate", arm))
        effect = means["candidate"]-means[arm]
        comparisons[arm] = {"effect": effect, "ci_low": effect-radius, "ci_high": effect+radius}
    verdict = ("HELPS" if all(r["ci_low"] > 0 for r in comparisons.values()) else
               "HURTS" if any(r["ci_high"] < 0 for r in comparisons.values()) else "NO_MEASURABLE_EFFECT")
    return {**report, "verdict": verdict, "comparisons": comparisons,
            "counts": {a: len(v) for a, v in groups.items()}, "interval": "fixed-horizon Hoeffding, joint alpha .05"}


def review(root: str, proposal_id: str, experiment_id: str, *, actor: str, expected_revision: str) -> dict:
    if not memory_authority.is_operator():
        raise PermissionError("compression admission requires an operator")
    with _jsonl.locked(_file(root)):
        row = next((r for r in memory_control.records(root, "compression") if r["id"] == proposal_id), None)
        if row is None or row["revision"] != expected_revision:
            raise ValueError("compression revision changed")
        data = row["data"]
        approval.check(approval.load_policy(root), slug=proposal_id, approver=actor, authors=(data["author"],))
        if actor == data["author"]:
            raise PermissionError("compression reviewer must be independent")
        report = evaluate(root, experiment_id)
        if report["proposal_sha256"] != policy.digest(row):
            raise ValueError("trial evidence belongs to a different proposal revision")
        if report["verdict"] == "UNIDENTIFIED":
            raise ValueError(report["reason"])
        status = "active" if report["verdict"] == "HELPS" else "demoted"
        admitted = memory_control.put(root, "compression", row["text"], record_id=proposal_id,
            labels=row["scopes"], actor=actor, data={**data, "status": status, "experiment": report})
        _append(root, {"id": uuid.uuid4().hex, "event": "admit", "proposal_id": proposal_id,
                       "experiment_id": experiment_id, "actor": actor, "admitted_sha256": policy.digest(admitted)})
        return admitted


def export_training(root: str, *, context: list[str] | None = None) -> dict:
    rows = active(root, context=context)
    examples = [{"prompt": r["data"]["applies_when"], "chosen": r["text"],
                 "sources": r["data"]["sources"], "experiment": r["data"]["experiment"]} for r in rows]
    return {"format": "evidence-linked-sft", "examples": examples, "training_performed": False}


def export_preferences(root: str, *, context: list[str] | None = None) -> dict:
    """DPO-style (prompt, chosen, rejected) pairs, each backed by a randomized comparison.

    Every registered compression trial randomizes a candidate against its
    adjacent parent and the raw source evidence. Where an arm comparison is
    decisive (its interval excludes zero), the better text is ``chosen`` and the
    worse ``rejected``, with the measured effect attached. Undecided
    comparisons yield nothing: a preference the evidence does not support is
    noise in a training set. Records or sources that were forgotten are left
    out. No training is performed.
    """
    pairs = []
    rows = {r["id"]: r for r in memory_control.records(root, "compression")}
    for experiment in (r for r in _events(root) if r["event"] == "register"):
        row = rows.get(experiment["proposal_id"])
        if row is None or memory_authority.lineage_blocked(root, row["id"]) or any(
                memory_authority.lineage_blocked(root, sid) for sid in row["data"]["sources"]):
            continue
        if context is not None and not memory_control.matches(row["scopes"], context):
            continue
        report = evaluate(root, experiment["id"])
        for arm, comparison in (report.get("comparisons") or {}).items():
            if comparison["ci_low"] > 0:
                chosen, rejected = experiment["contexts"]["candidate"], experiment["contexts"][arm]
            elif comparison["ci_high"] < 0:
                chosen, rejected = experiment["contexts"][arm], experiment["contexts"]["candidate"]
            else:
                continue
            if chosen == rejected:
                continue
            pairs.append({
                "prompt": row["data"]["applies_when"], "chosen": chosen, "rejected": rejected,
                "evidence": {"experiment": experiment["id"], "proposal": row["id"], "level": row["data"]["level"],
                             "against": arm, "effect": comparison["effect"],
                             "ci": [comparison["ci_low"], comparison["ci_high"]],
                             "counts": report.get("counts"), "interval": report.get("interval"),
                             "randomized": True}})
    return {"format": "evidence-linked-dpo", "pairs": pairs, "training_performed": False}
