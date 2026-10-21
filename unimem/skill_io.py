"""UniMem causal skill lifecycle, governed source reads and Agent Skills export.

Verifier tasks qualify candidates; only canonical randomized evidence can issue
promotion certificates. A lexical screen is a heuristic, and the conformal harm
bound requires exchangeability with its frozen, matched calibration traffic.
"""
from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from unimem import (
    _jsonl,
    aipw,
    conformal_gate,
    eprocess,
    frontmatter,
    holdout_io,
    memory_authority,
    memory_guard,
    paths,
    promotion,
    safety,
)
from unimem import frontier_protocol as fp
from unimem import nextwave_store as store
from unimem import skill_foundry as foundry
from unimem import transparency_log as log
from unimem import unit_trials as trials

KIND = "foundry-skills"


def _sources(root: str, ids: Sequence[str], context: Sequence[str], *, succeeded: bool) -> list[dict[str, Any]]:
    from unimem import ttl

    fp.count(len(ids), 1, 32)
    if len(set(ids)) != len(ids):
        raise ValueError("UniMem skill sources must be distinct measured trajectories")
    admitted = store.admit(root, ids, context=context)
    traces = store.traces(root)
    result = []
    for identity in ids:
        trace = traces.get(identity)
        receipt = (trace or {}).get("extensions", {}).get("profile", {}).get("origin", {})
        if (trace is None or identity not in admitted or not receipt
                or not memory_authority.verify(root, receipt, memory_authority.trace_record(trace))
                or receipt.get("authority") not in ("user", "operator")
                or not ttl.trace_is_live(trace) or trace.get("outcome", {}).get("resolved") is not succeeded):
            raise PermissionError("UniMem induction requires live, authenticated, scoped measured trajectories")
        result.append(trace)
    return result


def induce(root: str, name: str, description: str, trace_ids: Sequence[str], *, context: Sequence[str] = (),
           inducer: foundry.Inducer | None = None) -> dict[str, Any]:
    store.operator()
    fp.count(len(trace_ids), 1, 32)
    safety.assert_mutable(root, trace_ids)
    from unimem import sentinel_io

    sentinel_io.assert_generalize(root, trace_ids, context=context)
    traces = _sources(root, trace_ids, context, succeeded=True)
    value = foundry.candidate(traces, name=name, description=description, inducer=inducer)
    if memory_guard.scan_fields({"skill": value}).should_block:
        raise PermissionError("UniMem skill candidate failed the content-safety screen")
    identity = "skill:" + value["content_hash"]
    source_digests = {t["id"]: fp.digest(memory_authority.trace_record(t)) for t in traces}
    value = {"id": identity, **value, "source_digests": source_digests, "scopes": sorted(set(context))}
    prior = identity in store.latest(root, KIND)
    store.put(root, KIND, identity, value, scopes=context, sources=trace_ids, immutable=True)
    if not prior:
        store.event(root, "skill-state", identity, {"state": "candidate", "artifact_hash": value["content_hash"]})
    return value


def _load(root: str, identity: str, context: Sequence[str]) -> dict[str, Any]:
    value = store.get(root, KIND, identity, context=context)
    live = store.admit(root, list(value["source_digests"]), context=context)
    if any(i not in live or live[i].get("content_digest") != digest for i, digest in value["source_digests"].items()):
        raise PermissionError("UniMem skill source is unavailable or changed")
    if memory_authority.lineage_blocked(root, identity):
        raise PermissionError("UniMem skill is quarantined or erased")
    return value


def screen(root: str, identity: str, tasks: Sequence[dict[str, Any]], *, context: Sequence[str] = (),
           sandbox: foundry.Sandbox | None = None) -> dict[str, Any]:
    store.operator()
    fp.count(len(tasks), 1, 64)
    value = _load(root, identity, context)
    if identity in store.latest(root, "skill-screens"):
        old = store.get(root, "skill-screens", identity, context=context)
        if old["tasks_digest"] != fp.digest(list(tasks)):
            raise ValueError("UniMem skill verifier tasks are immutable")
        return dict(old["screen"])
    _sources(root, sorted({t["trace_id"] for t in tasks}), context, succeeded=False)
    result = foundry.prescreen(value, tasks, sandbox=sandbox)
    report = {"id": identity, "screen": result, "tasks_digest": fp.digest(list(tasks))}
    store.put(root, "skill-screens", identity, report,
              scopes=value["scopes"], sources=sorted(set(value["source_digests"]) | {t["trace_id"] for t in tasks}),
              immutable=True)
    store.event(root, "skill-state", identity, {"state": "screened" if result["passed"] else "rejected",
                                               "artifact_hash": value["content_hash"]})
    return result


def trial(root: str, identity: str, *, context: Sequence[str] = (), alpha: float = .05,
          rate: float = .5, training_size: int = 100, minimum_support: int = 10,
          bundle_ids: Sequence[str] = ()) -> dict[str, Any]:
    store.operator()
    fp.count(len(bundle_ids), 0, 64)
    value = _load(root, identity, context)
    screening = store.get(root, "skill-screens", identity, context=context)
    if not screening["screen"]["passed"]:
        raise PermissionError("UniMem rejected skill cannot receive live traffic")
    gate = conformal_gate.current(root)
    if not gate.get("enabled") or not gate.get("calibration_n"):
        raise ValueError("configure a frozen UniMem harm gate on matched ungated pilot traffic before a skill trial")
    members = sorted(set([identity, *bundle_ids]))
    fp.count(len(members), 4 if bundle_ids else 1, 64)
    values = [_load(root, i, context) for i in members]
    for member in values:
        screened = store.get(root, "skill-screens", member["id"], context=context)
        if not screened["screen"]["passed"] or member["scopes"] != value["scopes"]:
            raise PermissionError("UniMem bundle members require passing screens and identical scopes")
        state = store.latest(root, "skill-state").get(member["id"], {}).get("state")
        if state not in ("screened", "trial"):
            raise PermissionError("UniMem promoted or retired skills cannot restart their trial")
    bundle_hash = fp.digest(members) if bundle_ids else ""
    bundle_unit = "bundle:" + bundle_hash if bundle_ids else ""
    if bundle_unit:
        trials.register(root, bundle_unit, bundle_hash, alpha=alpha, rate=.5,
                        training_size=training_size, minimum_support=minimum_support)
    result: dict[str, Any] = {}
    for member in values:
        identity = member["id"]
        result = trials.register(root, identity, member["content_hash"], alpha=alpha, rate=rate,
                                 training_size=training_size, minimum_support=minimum_support)
        settings = {"id": identity, "gate_hash": fp.digest(gate), "bundle_unit": bundle_unit,
                    "members": members, "design": "bundle size then nested skill" if bundle_unit else "single factor",
                    "harm_scorer": "unimem-skill", "harm_score": 1.0}
        store.put(root, "skill-settings", identity, settings, scopes=member["scopes"],
                  sources=list(member["source_digests"]), immutable=True)
        store.event(root, "skill-state", identity, {"state": "trial", "artifact_hash": member["content_hash"]})
    return {**result, "bundle_unit": bundle_unit, "members": members}


def _settings(root: str, value: dict[str, Any], context: Sequence[str]) -> dict[str, Any]:
    return store.get(root, "skill-settings", value["id"], context=context)


def catalog(root: str, occasion: str, *, context: Sequence[str] = (), segment: str = "default",
            identities: Sequence[str] = ()) -> dict[str, Any]:
    """Return only actual eligible arms; no hidden skill text enters the response."""
    fp.text(occasion)
    segment = holdout_io.context_label(segment) or "default"
    fp.count(len(segment), 1, 51, "UniMem skill segment characters including room for the bundle factor")
    fp.count(len(identities), 0, 64)
    states = store.latest(root, "skill-state")
    requested = set(identities)
    result, design_log = [], []
    bundles: dict[str, dict[str, Any]] = {}
    considered = 0
    # Stable content ordering fixes eligibility before any factor is drawn.
    for identity, row in sorted(store.latest(root, KIND).items()):
        if requested and identity not in requested:
            continue
        if not store.visible(row.get("scopes", []), context) or states.get(identity, {}).get("state") not in (
                "trial", "promoted"):
            continue
        considered += 1
        fp.count(considered, 1, 64, "UniMem eligible catalog candidates; specify identities to narrow the catalog")
        try:
            value = _load(root, identity, context)
        except (PermissionError, ValueError):
            continue
        if states[identity]["state"] == "promoted":
            evidence(root, identity, context=context)  # Recheck revocation and source freshness.
            if retirement_signal(root, identity, context=context)["retire"]:
                continue
        settings = _settings(root, value, context)
        gate = conformal_gate.current(root)
        if fp.digest(gate) != settings["gate_hash"]:
            raise PermissionError("UniMem skill harm calibration changed; restart the preregistered trial")
        bundle_arm = None
        if settings["bundle_unit"]:
            if requested and not set(settings["members"]) <= requested:
                raise ValueError("UniMem bundle trials require the full preregistered candidate catalog")
            if settings["bundle_unit"] not in bundles:
                bundles[settings["bundle_unit"]] = trials.assign(
                    root, settings["bundle_unit"], occasion, context=segment)
            bundle_arm = bundles[settings["bundle_unit"]]
            # Size is chosen before the skill arm; masked skills have no memory-level assignment.
            size_limit = 64 if bundle_arm["injected"] else 3
            if settings["members"].index(identity) >= size_limit:
                trials.excluded(root, identity, occasion, "bundle-size eligibility", parent=settings["bundle_unit"])
                continue
        if not conformal_gate.admit(gate, settings["harm_score"], context=segment, scorer=settings["harm_scorer"]):
            trials.excluded(root, identity, occasion, "conformal harm gate", parent="harm-gate")
            continue
        trial_context = (segment + ("|bundle:large" if bundle_arm["injected"] else "|bundle:small")
                         if bundle_arm else segment)
        arm = trials.assign(root, identity, occasion, context=trial_context)
        design = {"unit": identity, "occasion": occasion, "skill_injected": arm["injected"],
                  "bundle_injected": bundle_arm["injected"] if bundle_arm else None,
                  "nesting": settings["design"], "context": trial_context, "gate_hash": settings["gate_hash"]}
        store.event(root, "skill-exposures", fp.digest([identity, occasion]), design)
        design_log.append(design)
        result.extend(foundry.exposed({**value, "presentation": "quoted-data", "trial": True}, arm["injected"]))
    return {"skills": result, "design": design_log, "classification": "estimate",
            "assumptions": ["catalog is the delivered treatment; caller must honor it",
                            "harm calibration and future catalog traffic are exchangeable"]}


def status(root: str, identity: str, *, context: Sequence[str] = (), pooled: bool = False) -> dict[str, Any]:
    value = _load(root, identity, context)
    state = store.latest(root, "skill-state").get(identity, {}).get("state", "candidate")
    result: dict[str, Any] = {"id": identity, "state": state, "classification": "heuristic"}
    if state in ("trial", "promoted", "retired"):
        result = {**result, **trials.report(root, identity)}
        if state == "promoted":
            result["retirement_signal"] = retirement_signal(root, identity, context=context)
        settings = _settings(root, value, context)
        if settings["bundle_unit"]:
            result["bundle_size_effect"] = trials.report(root, settings["bundle_unit"])
        if pooled:
            from unimem import pooled_io
            result["pooled"] = pooled_io.report(root, value["content_hash"])
    return result


def publish(root: str, identity: str, *, context: Sequence[str] = ()) -> dict[str, Any]:
    store.operator()
    from unimem import sentinel_io

    sentinel_io.assert_generalize(root, [identity, store.reference(KIND, identity)], context=context)
    from unimem import policy_io

    policy_io.assert_export(root, [store.reference(KIND, identity)], context=list(context))
    value = _load(root, identity, context)
    sentinel_io.require_update(root, value["content_hash"], context=context)
    state = store.latest(root, "skill-state").get(identity, {}).get("state")
    if state not in ("trial", "promoted"):
        raise PermissionError("UniMem publication requires a screened canonical trial")
    if retirement_signal(root, identity, context=context)["retire"]:
        raise PermissionError("UniMem harmful or decaying skills cannot be republished")
    settings = _settings(root, value, context)
    gate = conformal_gate.current(root)
    if fp.digest(gate) != settings["gate_hash"]:
        raise PermissionError("UniMem publication requires the trial's unchanged harm calibration")
    certificate = trials.certify(root, identity)
    report = trials.report(root, identity)
    rows = trials.rows(root, identity)
    failures = sum(bool(r["injected"] and not r["outcome"]) / (1-r["rate"]) for r in rows)
    expiry = (datetime.fromisoformat(fp.now()) + timedelta(days=30)).isoformat()
    signed = fp.sign(root, {"type": "unimem.skill-evidence", "version": 1, "id": identity,
        "artifact_hash": value["content_hash"], "effect": report["estimate"],
        "skill": {**foundry.metadata(value["name"], value["description"]), "format": value["format"]},
        "artifact_encoding": "SHA256 of the canonical JSON candidate-body string",
        "published_body_digest": fp.digest(foundry.document(value)),
        "signature_scope": ("public verifier with pinned Ed25519 key"
                            if memory_authority.signing_config(root)["algorithm"] == "ed25519"
                            else "shared-secret verifier; not public verification"),
        "interval": report["confidence_sequence"], "segments": sorted({r["context"] for r in rows}),
        "harm_budget": {"alpha": gate["alpha"], "observed_ipw_failure": failures / max(1, len(rows)),
                        "classification": "estimate", "calibration_hash": settings["gate_hash"]},
        "certificate_id": fp.digest(certificate["certificate"]), "certificate": certificate,
        "issued_at": fp.now(), "expires_at": expiry, "classification": "guarantee under trial assumptions"})
    anchor = log.append(root, "unimem-skill-evidence-v1", {"digest": fp.digest(signed)})
    bundle = {"signed": signed, "anchor": anchor}
    store.put(root, "skill-evidence", identity, bundle, scopes=value["scopes"], sources=list(value["source_digests"]))
    directory = os.path.join(root, "skills", value["name"])
    paths.enforce_boundary(root, directory)
    skill_path = os.path.join(directory, "SKILL.md")
    evidence_path = os.path.join(directory, "EVIDENCE.json")
    store.event(root, "skill-exports", value["name"], {"skill_id": identity,
                "sources": [identity], "scopes": value["scopes"]})
    # One lock covers the two-file export. Readers verify the evidence commitment.
    with _jsonl.locked(directory):
        if os.path.isfile(evidence_path):
            with open(evidence_path, "rb") as stream:
                raw = stream.read(store.MAX_BLOB + 1)
            if len(raw) > store.MAX_BLOB:
                raise ValueError("UniMem skill evidence file exceeds its size limit")
            prior = json.loads(raw)
            if prior.get("signed", {}).get("record", {}).get("id") != identity:
                raise ValueError("UniMem skill folder already belongs to different content")
        frontmatter.write(skill_path, foundry.metadata(value["name"], value["description"]),
                          foundry.document(value))
        _jsonl.write_json(evidence_path, bundle)
    old_state = store.latest(root, "skill-state").get(identity, {})
    store.event(root, "skill-state", identity, {"state": "promoted", "artifact_hash": value["content_hash"],
                "monitor_start": old_state.get("monitor_start", len(rows)),
                "baseline_lower": old_state.get("baseline_lower", max(0, report["confidence_sequence"][0]))})
    return {"folder": directory, **bundle}


def evidence(root: str, identity: str, *, context: Sequence[str] = ()) -> dict[str, Any]:
    value = _load(root, identity, context)
    if store.latest(root, "skill-state").get(identity, {}).get("state") != "promoted":
        raise PermissionError("UniMem skill is not promoted")
    result = store.get(root, "skill-evidence", identity, context=context)
    if fp.digest(conformal_gate.current(root)) != _settings(root, value, context)["gate_hash"]:
        raise PermissionError("UniMem skill harm calibration changed after publication")
    signed, anchor = result["signed"], result["anchor"]
    body = signed["record"]
    if (not fp.verify(root, signed) or body["type"] != "unimem.skill-evidence" or body["version"] != 1
            or fp.moment(body["expires_at"]) <= fp.now()
            or not log.verify(root, anchor) or anchor["statement"]["record"]["statement"]["digest"] != fp.digest(signed)
            or not promotion.verify(root, body["certificate"], artifact_hash=value["content_hash"])):
        raise PermissionError("UniMem skill evidence is expired, revoked or invalid")
    return result


def retirement_signal(root: str, identity: str, *, context: Sequence[str] = ()) -> dict[str, Any]:
    _load(root, identity, context)
    result = trials.report(root, identity)
    state = store.latest(root, "skill-state").get(identity, {})
    decay = 1.0
    if state.get("state") == "promoted":
        future = trials.rows(root, identity)[state["monitor_start"]:]
        observations = [aipw.Observation(r["injected"], bool(r["outcome"]), r["rate"]) for r in future]
        decay = eprocess.process(observations, direction="hurts", delta=-state["baseline_lower"]).maximum
    return {"retire": max(decay, result["harm_e_value"]) >= 2/result["alpha"],
            "harm_e_value": result["harm_e_value"], "decay_e_value": decay,
            "alpha": result["alpha"], "alarm_threshold": 2/result["alpha"],
            "classification": "guarantee under conditional directional null and exact propensities",
            "assumptions": ["post-promotion monitoring split fixed at publication", "complete canonical outcomes",
                            "Bonferroni allocation over harm and decay alarms"]}


def retire(root: str, identity: str, *, context: Sequence[str] = ()) -> dict[str, Any]:
    store.operator()
    value = _load(root, identity, context)
    result = retirement_signal(root, identity, context=context)
    if not result["retire"]:
        raise PermissionError("UniMem retirement requires canonical e-process harm or decay evidence")
    if identity in store.latest(root, "skill-evidence"):
        bundle = store.get(root, "skill-evidence", identity, context=context)
        if promotion.verify(root, bundle["signed"]["record"]["certificate"]):
            promotion.rollback(root, bundle["signed"]["record"]["certificate"], "canonical skill harm alarm")
    store.event(root, "skill-state", identity, {"state": "retired", "artifact_hash": value["content_hash"]})
    for name in ("SKILL.md", "EVIDENCE.json"):
        target = Path(root) / "skills" / value["name"] / name
        paths.enforce_boundary(root, str(target))
        target.unlink(missing_ok=True)
    return {"id": identity, "state": "retired", "e_value": max(result["harm_e_value"], result["decay_e_value"]),
            "classification": "estimate"}


def visible_published(root: str, filename: str, *, context: Sequence[str] = ()) -> bool:
    """A deleted evidence file cannot downgrade a managed skill into a legacy one."""
    index = store.latest(root, "skill-exports")
    name = Path(filename).parent.name
    if name not in index:
        return True
    if index[name].get("status") == "erased":
        return False
    identity = index[name]["skill_id"]
    try:
        value = _load(root, identity, context)
        certificate = evidence(root, identity, context=context)
        if retirement_signal(root, identity, context=context)["retire"]:
            return False
        expected = os.path.join(root, "skills", value["name"], "SKILL.md")
        if os.path.realpath(filename) != os.path.realpath(expected):
            return False
        paths.enforce_boundary(root, filename)
        card_path = os.path.join(os.path.dirname(filename), "EVIDENCE.json")
        paths.enforce_boundary(root, card_path)
        with open(card_path, "rb") as stream:
            raw = stream.read(store.MAX_BLOB + 1)
        if len(raw) > store.MAX_BLOB or fp.digest(json.loads(raw)) != fp.digest(certificate):
            return False
        fm, body = frontmatter.read(filename)
        return (fm == foundry.metadata(value["name"], value["description"])
                and fp.digest(body) == certificate["signed"]["record"]["published_body_digest"]
                and body.strip() == foundry.document(value).strip())
    except (ValueError, PermissionError, OSError, KeyError, TypeError):
        return False
