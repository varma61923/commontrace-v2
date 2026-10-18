"""Living, bitemporal safety-case evidence graph and scenario templates."""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any, cast

from commontrace import frontier_protocol as fp

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


def scenario_template(name: str, hazard: str, actor: str = "obstacle") -> str:
    name, actor = _name(name), _name(actor, "scenario actor")
    hazard = " ".join(fp.text(hazard, "hazard", 256).replace("#", "").split())
    # This generator emits a deliberately small OpenSCENARIO DSL 2.1 template.
    # Values are comments/evidence labels; behavior is a reusable abstract case.
    return (f"# Hazard: {hazard}\n"
            f"scenario {name}:\n"
            f"    sut: vehicle\n"
            f"    {actor}: vehicle\n"
            f"    do serial():\n"
            f"        setup: parallel():\n"
            f"            sut.vehicle.drive()\n"
            f"            {actor}.drive()\n"
            f"        hazardous_event: parallel(duration: [1s..4s]):\n"
            f"            sut.vehicle.drive()\n"
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
    required = ["    sut: vehicle", "    do serial():", "        setup: parallel():",
                "        hazardous_event: parallel(duration: [1s..4s]):", "                speed([0kph..5kph])"]
    if any(line not in lines for line in required):
        raise ValueError("scenario does not match the supported OpenSCENARIO syntax subset")
    if any(line.startswith(" ") and len(line) - len(line.lstrip(" ")) not in (4, 8, 12, 16)
           for line in lines[2:]):
        raise ValueError("scenario indentation is outside the supported subset")
    return {"valid": True, "scenario": match[1], "version": "2.1.0",
            "kind": "template subset syntax check; not full ASAM grammar or semantic validation"}


def add_claim(root: str, claim_id: str, *, hazard: str, statement: str, expected_evidence: int = 1) -> dict[str, Any]:
    claim_id, hazard = _name(claim_id), _name(hazard, "hazard identifier")
    statement = fp.text(statement, "safety claim", 2048)
    fp.count(expected_evidence, 1, 64)
    if any(row.get("node_id") == claim_id and row.get("type") == "claim" for row in fp.journal(root, JOURNAL)):
        raise ValueError("safety claim identifiers are immutable")
    body = {"node_id": claim_id, "type": "claim", "hazard": hazard, "statement": statement,
            "expected_evidence": expected_evidence, "at": fp.now()}
    return cast(dict[str, Any], fp.append(root, JOURNAL, body)["record"])


def add_argument(root: str, argument_id: str, *, claim_id: str, statement: str) -> dict[str, Any]:
    argument_id, claim_id = _name(argument_id), _name(claim_id, "claim id")
    if not any(row.get("node_id") == claim_id and row.get("type") == "claim" for row in fp.journal(root, JOURNAL)):
        raise KeyError("argument must link an existing safety claim")
    body = {"node_id": argument_id, "type": "argument", "claim_id": claim_id,
            "statement": fp.text(statement, "safety argument", 2048), "at": fp.now()}
    return cast(dict[str, Any], fp.append(root, JOURNAL, body)["record"])


def add_evidence(root: str, evidence_id: str, *, claim_id: str, source_type: str, source_id: str,
                 source_digest: str, dependencies: Sequence[dict[str, str]], strength: float = 1) -> dict[str, Any]:
    evidence_id, claim_id = _name(evidence_id, "evidence id"), _name(claim_id, "claim id")
    if not any(row.get("node_id") == claim_id and row.get("type") == "claim" for row in fp.journal(root, JOURNAL)):
        raise KeyError("evidence must link an existing safety claim")
    if source_type not in SOURCE_TYPES:
        raise ValueError("unsupported safety-case evidence source type")
    fp.text(source_id, "evidence source id", 256)
    if len(source_digest) != 64 or any(ch not in "0123456789abcdef" for ch in source_digest):
        raise ValueError("evidence source digest must be lowercase SHA-256")
    deps = []
    for dependency in dependencies:
        if set(dependency) != {"kind", "digest"} or dependency["kind"] not in DEPENDENCIES:
            raise ValueError("evidence dependencies must name model, software or world_model digests")
        deps.append({"kind": dependency["kind"], "digest": _digest(dependency["digest"])})
    if len({(item["kind"], item["digest"]) for item in deps}) != len(deps):
        raise ValueError("safety evidence dependency references must be unique")
    body = {"node_id": evidence_id, "type": "evidence", "claim_id": claim_id,
            "source_type": source_type, "source_id": source_id, "source_digest": source_digest,
            "dependencies": sorted(deps, key=lambda item: (item["kind"], item["digest"])),
            "strength": fp.number(strength, 0, 1), "stale": False, "at": fp.now()}
    if any(row.get("node_id") == evidence_id and row.get("type") == "evidence" for row in fp.journal(root, JOURNAL)):
        raise ValueError("evidence identifiers are immutable")
    return cast(dict[str, Any], fp.append(root, JOURNAL, body)["record"])


def _digest(value: str) -> str:
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError("dependency digest must be lowercase SHA-256")
    return value


def mine(root: str, event: dict[str, Any], *, claim_id: str, replay: Callable[[dict[str, Any]], dict[str, Any]],
         dependencies: Sequence[dict[str, str]], minimum_strength: float = .5) -> dict[str, Any]:
    """Persist only reproducible failures or near-failures with two equal replay outputs."""
    if event.get("failed") is not True and event.get("near_failure") is not True:
        return {"kept": False, "reason": "not a failure or near-failure"}
    fp.wire(event)
    first, second = replay(event), replay(event)
    fp.wire(first)
    fp.wire(second)
    if not isinstance(first, dict) or not isinstance(second, dict) or fp.digest(first) != fp.digest(second):
        return {"kept": False, "reason": "replay was not reproducible"}
    if first.get("failed") is not True and first.get("near_failure") is not True:
        return {"kept": False, "reason": "replay did not reproduce failure or near-failure"}
    name = "scenario_" + fp.digest([event, first])[:24]
    text = scenario_template(name, str(event.get("hazard", "unspecified hazard")))
    validate_scenario(text)
    source_id = fp.text(str(event.get("episode_id", event.get("id", ""))), "episode id", 256)
    evidence_id = "evidence_" + fp.digest([source_id, first])[:24]
    body = {"node_id": name, "type": "scenario", "hazard": event.get("hazard"), "source_id": source_id,
            "source_digest": fp.digest(event), "replay_digest": fp.digest(first), "text": text,
            "reproduced": True, "at": fp.now()}
    fp.append(root, JOURNAL, body)
    evidence = add_evidence(root, evidence_id, claim_id=claim_id, source_type="scenario", source_id=name,
        source_digest=fp.digest(text), dependencies=dependencies, strength=fp.number(minimum_strength, 0, 1))
    return {"kept": True, "scenario_id": name, "evidence_id": evidence["node_id"],
            "reproductions": 2, "text": text}


def mark_changed(root: str, *, kind: str, digest: str) -> list[dict[str, Any]]:
    if kind not in DEPENDENCIES:
        raise ValueError("dependency kind must be model, software or world_model")
    digest = _digest(digest)
    events = fp.journal(root, JOURNAL)
    stale = {row["node_id"]: row for row in events if row.get("type") == "evidence" and row.get("stale")}
    latest = {}
    for row in events:
        if row.get("type") == "evidence":
            latest[row["node_id"]] = row
    changed = []
    for identity, row in sorted(latest.items()):
        if identity in stale or not any(dep["kind"] == kind and dep["digest"] == digest
                                        for dep in row["dependencies"]):
            continue
        event = {"node_id": identity, "type": "evidence", "claim_id": row["claim_id"],
                 "source_type": row["source_type"], "source_id": row["source_id"],
                 "source_digest": row["source_digest"], "dependencies": row["dependencies"],
                 "strength": row["strength"], "stale": True, "stale_reason": kind + " changed",
                 "at": fp.now()}
        signed = fp.append(root, JOURNAL, event)
        queue = fp.append(root, REVALIDATION, {"evidence_id": identity, "dependency": {"kind": kind,
            "digest": digest}, "status": "pending-sim-revalidation", "at": fp.now()})
        changed.append({"evidence_id": identity, "stale_sequence": signed["record"]["sequence"],
                        "queue_sequence": queue["record"]["sequence"]})
    return changed


def queue_gate(root: str, evidence_id: str, *, memory_id: str, model_digest: str, embodiment: str, task: str,
               state: dict[str, Any], action: dict[str, Any], perturbations: list[dict[str, Any]],
               adapter: Any = None) -> dict[str, Any]:
    """Resolve a queued evidence refresh through G23's action-sensitive model gate."""
    queued = [row for row in fp.journal(root, REVALIDATION) if row["evidence_id"] == evidence_id
              and row["status"] == "pending-sim-revalidation"]
    if not queued:
        raise KeyError("no pending safety evidence revalidation")
    from commontrace import world_model_trust

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
        evidences = [row for row in latest.values() if row["claim_id"] in {c["node_id"] for c in related_claims}]
        active = [row for row in evidences if not row["stale"] and row["strength"] >= minimum_strength]
        stale = [row["node_id"] for row in evidences if row["stale"]]
        weak = [row["node_id"] for row in evidences if not row["stale"] and row["strength"] < minimum_strength]
        expected = sum(row["expected_evidence"] for row in related_claims)
        report.append({"hazard": hazard, "claims": len(related_claims), "active_evidence": len(active),
            "expected_evidence": expected, "weak": weak, "stale": stale,
            "missing": not related_claims or len(active) < expected,
            "coverage": min(1.0, len(active) / expected) if expected else 0.0})
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
