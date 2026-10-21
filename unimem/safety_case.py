"""Living, bitemporal safety-case evidence graph and scenario templates."""
from __future__ import annotations

import copy
import re
from collections.abc import Callable, Sequence
from typing import Any, cast

from unimem import _jsonl
from unimem import frontier_protocol as fp

JOURNAL = "robot-safety-case.jsonl"
REVALIDATION = "robot-safety-case-revalidation.jsonl"
SOURCE_TYPES = {"episode", "trial", "certificate", "scenario"}
DEPENDENCIES = {"model", "software", "world_model"}


def build(root: str, *, claims: Sequence[dict[str, Any]], arguments: Sequence[dict[str, Any]] = (),
          evidence: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    """Add a bounded initial graph in dependency order from a reviewed JSON body."""
    if (not isinstance(claims, (list, tuple)) or not isinstance(arguments, (list, tuple))
            or not isinstance(evidence, (list, tuple))):
        raise ValueError("safety-case graph collections must be arrays")
    fp.count(len(claims), 1, 256)
    fp.count(len(arguments), 0, 1024)
    fp.count(len(evidence), 0, 4096)
    created_claims = [add_claim(root, **row) for row in claims]
    created_arguments = [add_argument(root, **row) for row in arguments]
    created_evidence = [add_evidence(root, **row) for row in evidence]
    return {"claims": created_claims, "arguments": created_arguments, "evidence": created_evidence}


def _name(value: str, label: str = "safety-case name") -> str:
    value = fp.text(value, label, 96)
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        raise ValueError(f"{label} must be a lowercase identifier")
    return value


def _append_node(root: str, body: dict[str, Any]) -> dict[str, Any]:
    with _jsonl.locked(fp.filename(root, JOURNAL)):
        events = fp.journal(root, JOURNAL)
        if any(row.get("node_id") == body["node_id"] for row in events):
            raise ValueError("safety-case node identifiers are immutable and unique")
        if body["type"] in {"argument", "evidence"} and not any(
                row.get("type") == "claim" and row.get("node_id") == body["claim_id"] for row in events):
            raise KeyError("safety-case node must link an existing safety claim")
        return cast(dict[str, Any], fp.append(root, JOURNAL, body)["record"])


def _dependencies(dependencies: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    if not isinstance(dependencies, (list, tuple)) or len(dependencies) > 64:
        raise ValueError("safety evidence dependencies must be an array of at most 64 items")
    deps = []
    for dependency in dependencies:
        if (not isinstance(dependency, dict) or set(dependency) != {"kind", "digest"}
                or not isinstance(dependency["kind"], str) or dependency["kind"] not in DEPENDENCIES):
            raise ValueError("evidence dependencies must name model, software or world_model digests")
        deps.append({"kind": dependency["kind"], "digest": _digest(dependency["digest"])})
    if len({(item["kind"], item["digest"]) for item in deps}) != len(deps):
        raise ValueError("safety evidence dependency references must be unique")
    return sorted(deps, key=lambda item: (item["kind"], item["digest"]))


def scenario_template(name: str, hazard: str, actor: str = "obstacle") -> str:
    name, actor = _name(name), _name(actor, "scenario actor")
    if actor == "sut":
        raise ValueError("scenario actor must be distinct from the system under test")
    hazard = " ".join(fp.text(hazard, "hazard", 256).replace("#", "").split())
    # This generator emits a deliberately small OpenSCENARIO DSL 2.1 template.
    # Values are comments/evidence labels; behavior is a reusable abstract case.
    return (f"# Hazard: {hazard}\n"
            f"scenario {name}:\n"
            f"    sut: vehicle\n"
            f"    {actor}: vehicle\n"
            f"    do serial():\n"
            f"        setup: parallel():\n"
            f"            sut.drive()\n"
            f"            {actor}.drive()\n"
            f"        hazardous_event: parallel(duration: [1s..4s]):\n"
            f"            sut.drive()\n"
            f"            {actor}.drive() with:\n"
            f"                speed([0kph..5kph])\n")


def validate_scenario(source: str) -> dict[str, Any]:
    """Validate the generator's documented syntax subset, not full ASAM semantics."""
    fp.text(source, "OpenSCENARIO template", 32_768)
    lines = source.splitlines()
    if len(lines) < 10 or not re.fullmatch(r"# Hazard: [^\n]+", lines[0]):
        raise ValueError("scenario is outside the documented OpenSCENARIO template subset")
    match = re.fullmatch(r"scenario ([a-z][a-z0-9_]{0,63}):", lines[1])
    if match is None:
        raise ValueError("OpenSCENARIO scenario declaration is malformed")
    actor = re.fullmatch(r"    ([a-z][a-z0-9_]{0,63}): vehicle", lines[3])
    if actor is None:
        raise ValueError("scenario does not match the supported OpenSCENARIO syntax subset")
    expected = scenario_template(match[1], lines[0][len("# Hazard: "):], actor[1])
    if source.rstrip("\n") != expected.rstrip("\n"):
        raise ValueError("scenario does not match the supported OpenSCENARIO syntax subset")
    return {"valid": True, "scenario": match[1], "version": "2.1.0",
            "kind": "template subset syntax check; not full ASAM grammar or semantic validation"}


def add_claim(root: str, claim_id: str, *, hazard: str, statement: str, expected_evidence: int = 1) -> dict[str, Any]:
    claim_id, hazard = _name(claim_id), _name(hazard, "hazard identifier")
    statement = fp.text(statement, "safety claim", 2048)
    fp.count(expected_evidence, 1, 64)
    body = {"node_id": claim_id, "type": "claim", "hazard": hazard, "statement": statement,
            "expected_evidence": expected_evidence, "at": fp.now()}
    return _append_node(root, body)


def add_argument(root: str, argument_id: str, *, claim_id: str, statement: str) -> dict[str, Any]:
    argument_id, claim_id = _name(argument_id), _name(claim_id, "claim id")
    body = {"node_id": argument_id, "type": "argument", "claim_id": claim_id,
            "statement": fp.text(statement, "safety argument", 2048), "at": fp.now()}
    return _append_node(root, body)


def add_evidence(root: str, evidence_id: str, *, claim_id: str, source_type: str, source_id: str,
                 source_digest: str, dependencies: Sequence[dict[str, str]], strength: float = 1) -> dict[str, Any]:
    evidence_id, claim_id = _name(evidence_id, "evidence id"), _name(claim_id, "claim id")
    if not isinstance(source_type, str) or source_type not in SOURCE_TYPES:
        raise ValueError("unsupported safety-case evidence source type")
    fp.text(source_id, "evidence source id", 256)
    source_digest = _digest(source_digest)
    deps = _dependencies(dependencies)
    body = {"node_id": evidence_id, "type": "evidence", "claim_id": claim_id,
            "source_type": source_type, "source_id": source_id, "source_digest": source_digest,
            "dependencies": sorted(deps, key=lambda item: (item["kind"], item["digest"])),
            "strength": fp.number(strength, 0, 1), "stale": False, "at": fp.now()}
    return _append_node(root, body)


def _digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("dependency digest must be lowercase SHA-256")
    return value


def mine(root: str, event: dict[str, Any], *, claim_id: str, replay: Callable[[dict[str, Any]], dict[str, Any]],
         dependencies: Sequence[dict[str, str]], minimum_strength: float = .5) -> dict[str, Any]:
    """Persist only reproducible failures or near-failures with two equal replay outputs."""
    if not isinstance(event, dict):
        raise ValueError("safety field event must be an object")
    if event.get("failed") is not True and event.get("near_failure") is not True:
        return {"kept": False, "reason": "not a failure or near-failure"}
    fp.wire(event)
    event = copy.deepcopy(event)
    dependencies = _dependencies(dependencies)
    minimum_strength = fp.number(minimum_strength, 0, 1)
    first = copy.deepcopy(replay(copy.deepcopy(event)))
    second = copy.deepcopy(replay(copy.deepcopy(event)))
    fp.wire(first)
    fp.wire(second)
    if not isinstance(first, dict) or not isinstance(second, dict) or fp.digest(first) != fp.digest(second):
        return {"kept": False, "reason": "replay was not reproducible"}
    if first.get("failed") is not True and first.get("near_failure") is not True:
        return {"kept": False, "reason": "replay did not reproduce failure or near-failure"}
    name = "scenario_" + fp.digest([event, first])[:24]
    text = scenario_template(name, event.get("hazard", "unspecified hazard"))
    validate_scenario(text)
    source_id = fp.text(event.get("episode_id", event.get("id", "")), "episode id", 256)
    evidence_id = "evidence_" + fp.digest([source_id, first])[:24]
    body = {"node_id": name, "type": "scenario", "hazard": event.get("hazard"), "source_id": source_id,
            "source_digest": fp.digest(event), "replay_digest": fp.digest(first), "text": text,
            "reproduced": True, "at": fp.now()}
    with _jsonl.locked(fp.filename(root, JOURNAL)):
        events = fp.journal(root, JOURNAL)
        if not any(row.get("type") == "claim" and row.get("node_id") == claim_id for row in events):
            raise KeyError("scenario must link an existing safety claim")
        existing = next((row for row in events if row.get("type") == "evidence"
                         and row.get("node_id") == evidence_id), None)
        if existing is not None and existing["source_id"] != name:
            # Preserve legacy IDs while separating distinct events from one
            # episode that happen to produce the same replay result.
            evidence_id = "evidence2_" + fp.digest(["scenario-evidence-v2", name, source_id, first])[:24]
            existing = next((row for row in events if row.get("type") == "evidence"
                             and row.get("node_id") == evidence_id), None)
        if existing is not None:
            if (existing["claim_id"] != claim_id or existing["dependencies"] != list(dependencies)
                    or existing["source_id"] != name):
                raise ValueError("scenario replay is already linked to different evidence")
            return {"kept": True, "scenario_id": name, "evidence_id": evidence_id,
                    "reproductions": 2, "text": text}
        prior_scenario = next((row for row in events if row.get("node_id") == name), None)
        if prior_scenario is not None and (prior_scenario.get("type") != "scenario"
                or prior_scenario.get("source_digest") != body["source_digest"]):
            raise ValueError("scenario node identifier is already bound to different content")
        if prior_scenario is None:
            _append_node(root, body)
        evidence = add_evidence(root, evidence_id, claim_id=claim_id, source_type="scenario", source_id=name,
            source_digest=fp.digest(text), dependencies=dependencies, strength=minimum_strength)
    return {"kept": True, "scenario_id": name, "evidence_id": evidence["node_id"],
            "reproductions": 2, "text": text}


def mark_changed(root: str, *, kind: str, digest: str) -> list[dict[str, Any]]:
    with _jsonl.locked(fp.filename(root, JOURNAL)):
        return _mark_changed(root, kind=kind, digest=digest)


def _mark_changed(root: str, *, kind: str, digest: str) -> list[dict[str, Any]]:
    if not isinstance(kind, str) or kind not in DEPENDENCIES:
        raise ValueError("dependency kind must be model, software or world_model")
    digest = _digest(digest)
    events = fp.journal(root, JOURNAL)
    stale = {row["node_id"]: row for row in events if row.get("type") == "evidence" and row.get("stale")}
    latest = {}
    for row in events:
        if row.get("type") == "evidence":
            latest[row["node_id"]] = row
    queued = {(row["evidence_id"], row["dependency"]["kind"], row["dependency"]["digest"])
              for row in fp.journal(root, REVALIDATION) if "dependency" in row}
    changed = []
    for identity, row in sorted(latest.items()):
        if not any(dep["kind"] == kind and dep["digest"] == digest for dep in row["dependencies"]):
            continue
        if identity in stale:
            if (identity, kind, digest) in queued:
                continue
            # The stale record is a durable outbox after an interrupted queue
            # append; a retry repairs the missing request without another stale event.
            stale_sequence = row["sequence"]
        else:
            event = {"node_id": identity, "type": "evidence", "claim_id": row["claim_id"],
                     "source_type": row["source_type"], "source_id": row["source_id"],
                     "source_digest": row["source_digest"], "dependencies": row["dependencies"],
                     "strength": row["strength"], "stale": True, "stale_reason": kind + " changed",
                     "at": fp.now()}
            stale_sequence = fp.append(root, JOURNAL, event)["record"]["sequence"]
        queue = fp.append(root, REVALIDATION, {"evidence_id": identity, "dependency": {"kind": kind,
            "digest": digest}, "status": "pending-sim-revalidation", "at": fp.now()})
        changed.append({"evidence_id": identity, "stale_sequence": stale_sequence,
                        "queue_sequence": queue["record"]["sequence"]})
    return changed


def queue_gate(root: str, evidence_id: str, *, memory_id: str, model_digest: str, embodiment: str, task: str,
               state: dict[str, Any], action: dict[str, Any], perturbations: list[dict[str, Any]],
               adapter: Any = None) -> dict[str, Any]:
    """Resolve a queued evidence refresh through G23's action-sensitive model gate."""
    with _jsonl.locked(fp.filename(root, REVALIDATION)):
        return _queue_gate(root, evidence_id, memory_id=memory_id, model_digest=model_digest,
                           embodiment=embodiment, task=task, state=state, action=action,
                           perturbations=perturbations, adapter=adapter)


def _queue_gate(root: str, evidence_id: str, *, memory_id: str, model_digest: str, embodiment: str, task: str,
                state: dict[str, Any], action: dict[str, Any], perturbations: list[dict[str, Any]],
                adapter: Any) -> dict[str, Any]:
    queued = [row for row in fp.journal(root, REVALIDATION) if row["evidence_id"] == evidence_id]
    if not queued or queued[-1]["status"] != "pending-sim-revalidation":
        raise KeyError("no pending safety evidence revalidation")
    from unimem import world_model_trust

    result = world_model_trust.gate(root, memory_id, model_digest=model_digest, embodiment=embodiment,
        task=task, state=state, action=action, perturbations=perturbations, adapter=adapter)
    status = (
        "sim-veto" if result["decision"] == "veto"
        else "sim-held" if result["decision"] == "hold"
        else "real-trial-required"
    )
    fp.append(root, REVALIDATION, {"evidence_id": evidence_id, "status": status,
                                   "gate_digest": fp.digest(result), "at": fp.now()})
    return {"evidence_id": evidence_id, "status": status, "gate": result}


def coverage(root: str, *, required_hazards: Sequence[str], minimum_strength: float = .8) -> dict[str, Any]:
    if not isinstance(required_hazards, (list, tuple)):
        raise ValueError("required hazards must be an array")
    fp.count(len(required_hazards), 1, 1024)
    minimum_strength = fp.number(minimum_strength, 0, 1)
    latest, claims = {}, {}
    for row in fp.journal(root, JOURNAL):
        if row.get("type") == "claim":
            claims[row["node_id"]] = row
        elif row.get("type") == "evidence":
            latest[row["node_id"]] = row
    requested = sorted(set(_name(item, "hazard") for item in required_hazards))
    report = []
    for hazard in requested:
        related_claims = [row for row in claims.values() if row["hazard"] == hazard]
        claim_ids = {claim["node_id"] for claim in related_claims}
        evidences = [row for row in latest.values() if row["claim_id"] in claim_ids]
        active = [row for row in evidences if not row["stale"] and row["strength"] >= minimum_strength]
        stale = [row["node_id"] for row in evidences if row["stale"]]
        weak = [row["node_id"] for row in evidences if not row["stale"] and row["strength"] < minimum_strength]
        expected = sum(row["expected_evidence"] for row in related_claims)
        missing_claims = []
        credited = 0
        sources_by_claim: dict[str, set[str]] = {}
        for row in active:
            sources_by_claim.setdefault(row["claim_id"], set()).add(row["source_digest"])
        for claim in related_claims:
            sources = sources_by_claim.get(claim["node_id"], set())
            credited += min(len(sources), claim["expected_evidence"])
            if len(sources) < claim["expected_evidence"]:
                missing_claims.append(claim["node_id"])
        report.append({"hazard": hazard, "claims": len(related_claims), "active_evidence": len(active),
            "expected_evidence": expected, "weak": weak, "stale": stale,
            "missing": not related_claims or bool(missing_claims), "missing_claims": sorted(missing_claims),
            "coverage": credited / expected if expected else 0.0})
    return {"hazards": report,
            "gaps": [row["hazard"] for row in report if row["missing"] or row["weak"] or row["stale"]],
            "kind": "evidence coverage summary; not a safety certification"}


def export_scenario(root: str, scenario_id: str) -> dict[str, Any]:
    scenario_id = _name(scenario_id, "scenario id")
    rows = [row for row in fp.journal(root, JOURNAL) if row.get("node_id") == scenario_id
            and row.get("type") == "scenario"]
    if not rows:
        raise KeyError("scenario template is unavailable")
    result = validate_scenario(rows[-1]["text"])
    return {"scenario_id": scenario_id, "text": rows[-1]["text"], "validation": result}
